"""
Vendor order comparison - Streamlit app.

Connect to PostgreSQL, pick a database from the list the server returns, choose
vendor tables and an optional state filter, then see which order lists get the
most sales and transfers (matched by phone number against the CRM).

Privacy: the app shows and exports counts and rates only, never phone numbers
or names.
"""

import io
import os
import re

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import psycopg2
import streamlit as st
from psycopg2 import sql

# Database configuration - priority: secrets > environment variables > defaults
def get_db_config():
    """Get database configuration from Streamlit secrets or environment variables."""
    return {
        "HOST": st.secrets.get("PG_HOST", os.environ.get("PG_HOST", "10.97.10.50")),
        "PORT": st.secrets.get("PG_PORT", os.environ.get("PG_PORT", "5432")),
        "USER": st.secrets.get("PG_USER", os.environ.get("PG_USER", "postgres")),
        "SCHEMA": st.secrets.get("PG_SCHEMA", os.environ.get("PG_SCHEMA", "public")),
        "PASSWORD": st.secrets.get("PG_PASSWORD", os.environ.get("PG_PASSWORD", ""))
    }

SALE_STATUSES = {"approved", "pending client reply", "pending bank approval", "postdated"}
PHONE_NAME_HINTS = ["phone_number", "phone", "mobile", "telephone", "tel", "contact_number"]
STATE_NAME_HINTS = ["state", "st", "state_code", "province"]

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


# ==================== CRM Cleaning ====================
@st.cache_data(show_spinner="Cleaning CRM file...")
def clean_crm(file_bytes):
    """
    Clean CRM data according to all requirements.
    Returns a tuple: (crm_index, crm_stats, date_ranges)
    """
    # Read CRM file
    crm = pd.read_csv(io.BytesIO(file_bytes), low_memory=False, dtype={"Phone Number": str})
    
    # Check required columns
    required_cols = {"Phone Number", "Closing Status", "Opening Status", "State", "Pricing List", 
                     "Creation Date", "Date of Sale"}
    missing = required_cols - set(crm.columns)
    if missing:
        raise ValueError(f"CRM file is missing required columns: {', '.join(sorted(missing))}")
    
    initial_rows = len(crm)
    
    # a) Phone numbers
    def clean_phone(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        text = str(value)
        # Remove trailing .0
        if re.fullmatch(r"\d+\.0", text):
            text = text[:-2]
        # Strip everything except digits
        digits = re.sub(r"\D", "", text)
        # Keep last 10 digits
        if len(digits) < 10:
            return None
        return digits[-10:]
    
    crm["phone10"] = crm["Phone Number"].map(clean_phone)
    crm = crm.dropna(subset=["phone10"])
    
    # b) Closing Status
    closing_status = crm["Closing Status"].fillna("").str.strip().str.lower()
    crm["is_sale"] = closing_status.isin(SALE_STATUSES)
    
    # c) Opening Status
    opening_status = crm["Opening Status"].fillna("").str.strip().str.lower()
    crm["is_transfer"] = opening_status.eq("transferred")
    crm["is_red_flag"] = opening_status.eq("red flag")
    
    # d) State
    def norm_state(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "Unknown"
        text = re.sub(r"\s+", " ", str(value).strip())
        lower = text.lower()
        if lower in US_STATES:
            return US_STATES[lower]
        if text.upper() in STATE_CODES:
            return text.upper()
        # Extract from end of value like "San Diego, CA"
        m = re.search(r",\s*([A-Za-z]{2})$", text)
        if m and m.group(1).upper() in STATE_CODES:
            return m.group(1).upper()
        return "Unknown"
    
    crm["state_clean"] = crm["State"].map(norm_state)
    
    # e) Pricing List
    def parse_price(value):
        if pd.isna(value):
            return None
        text = str(value).strip()
        # Parse "$44.95/Month" -> 44.95
        m = re.match(r"\$?([\d,]+\.?\d*)", text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                return None
        return None
    
    crm["price"] = crm["Pricing List"].map(parse_price)
    
    # f) Dates (parse with errors="coerce" for invalid dates like "0000-00-00")
    crm["creation_date"] = pd.to_datetime(crm["Creation Date"], errors="coerce")
    crm["sale_date"] = pd.to_datetime(crm["Date of Sale"], errors="coerce")
    
    # g) One row per phone
    crm_index = crm.groupby("phone10").agg(
        is_sale=("is_sale", "max"),
        is_transfer=("is_transfer", "max"),
        is_red_flag=("is_red_flag", "max"),
        state=("state_clean", lambda x: next((s for s in x if s != "Unknown"), "Unknown")),
        price=("price", "first"),  # Keep price for sale rows
    ).reset_index()
    
    # Calculate stats
    total_sales = int(crm_index["is_sale"].sum())
    total_transfers = int(crm_index["is_transfer"].sum())
    total_red_flags = int(crm_index["is_red_flag"].sum())
    unknown_state_pct = (crm_index["state"] == "Unknown").mean() * 100
    
    # Monthly sales value
    sale_phones = crm_index[crm_index["is_sale"]]
    monthly_sales_value = sale_phones["price"].sum()
    sales_without_price = sale_phones["price"].isna().sum()
    
    # Date ranges
    creation_range = (crm["creation_date"].min(), crm["creation_date"].max())
    sale_range = (crm["sale_date"].min(), crm["sale_date"].max())
    
    crm_stats = {
        "initial_rows": initial_rows,
        "after_phone_clean": len(crm),
        "unique_phones": len(crm_index),
        "sale_rows": int(crm["is_sale"].sum()),
        "transfer_rows": int(crm["is_transfer"].sum()),
        "red_flag_rows": int(crm["is_red_flag"].sum()),
        "unique_sale_phones": total_sales,
        "unique_transfer_phones": total_transfers,
        "unique_red_flag_phones": total_red_flags,
        "unknown_state_pct": unknown_state_pct,
        "monthly_sales_value": monthly_sales_value,
        "sales_without_price": sales_without_price,
    }
    
    date_ranges = {
        "creation_date": creation_range,
        "sale_date": sale_range,
    }
    
    return crm_index, crm_stats, date_ranges


# ==================== Database Helpers ====================
def connect(host, port, user, password, database):
    return psycopg2.connect(
        host=host, port=int(port), dbname=database, user=user,
        password=password, connect_timeout=10,
    )


def list_databases(host, port, user, password):
    conn = connect(host, port, user, password, "postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT datname FROM pg_database "
                "WHERE datistemplate = false AND datallowconn ORDER BY datname"
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def list_tables(conn, schema):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (schema,),
        )
        return [r[0] for r in cur.fetchall()]


def load_table(conn, schema, table, columns=None):
    if columns:
        query = sql.SQL("SELECT {} FROM {}.{}").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.Identifier(schema),
            sql.Identifier(table)
        )
    else:
        query = sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier(schema),
            sql.Identifier(table)
        )
    return pd.read_sql(query.as_string(conn), conn)


# ==================== Column Detection ====================
def detect_phone_column(df):
    """Detect phone column by name first, then by values."""
    best_col, best_score = None, 0.0
    
    for col in df.columns:
        name = str(col).lower().strip()
        # Name-based scoring
        if name in PHONE_NAME_HINTS:
            name_score = 1.0
        elif any(hint in name for hint in ["phone", "mobile"]):
            name_score = 0.5
        else:
            name_score = 0.0
        
        # Value-based scoring
        sample = df[col].dropna().head(300)
        if sample.empty:
            continue
        
        def clean_test(value):
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return None
            text = str(value)
            if re.fullmatch(r"\d+\.0", text):
                text = text[:-2]
            digits = re.sub(r"\D", "", text)
            return digits if len(digits) >= 10 else None
        
        looks_like_phone = sample.map(clean_test).map(lambda x: x is not None).mean()
        
        if looks_like_phone < 0.6:
            continue
        
        score = name_score + looks_like_phone
        if score > best_score:
            best_col, best_score = col, score
    
    return best_col


def detect_state_column(df):
    """Detect state column by name first, then by values."""
    best_col, best_score = None, 0.0
    
    for col in df.columns:
        name = str(col).lower().strip()
        # Name-based scoring
        if name in STATE_NAME_HINTS:
            name_score = 1.0
        elif "state" in name:
            name_score = 0.5
        else:
            name_score = 0.0
        
        # Value-based scoring
        sample = df[col].dropna().head(300)
        if sample.empty:
            continue
        
        def norm_test(value):
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return None
            text = re.sub(r"\s+", " ", str(value).strip())
            lower = text.lower()
            if lower in US_STATES:
                return US_STATES[lower]
            if text.upper() in STATE_CODES:
                return text.upper()
            m = re.search(r",\s*([A-Za-z]{2})$", text)
            if m and m.group(1).upper() in STATE_CODES:
                return m.group(1).upper()
            return None
        
        looks_like_state = sample.map(norm_test).map(lambda x: x is not None).mean()
        
        if looks_like_state < 0.5:
            continue
        
        score = name_score + looks_like_state
        if score > best_score:
            best_col, best_score = col, score
    
    return best_col


# ==================== Analysis ====================
def analyse_table(conn, schema, table, crm_index, states, date_filter):
    """
    Analyse a single vendor table.
    Returns: (summary_dict, by_state_df, error_message)
    """
    try:
        # Detect columns first to only load needed columns
        # Load sample to detect columns
        sample_query = sql.SQL("SELECT * FROM {}.{} LIMIT 1000").format(
            sql.Identifier(schema),
            sql.Identifier(table)
        )
        sample_df = pd.read_sql(sample_query.as_string(conn), conn)
        
        phone_col = detect_phone_column(sample_df)
        if phone_col is None:
            return None, None, "no phone column detected"
        
        state_col = detect_state_column(sample_df)
        
        # Load only needed columns
        columns_to_load = [phone_col]
        if state_col:
            columns_to_load.append(state_col)
        
        df = load_table(conn, schema, table, columns=columns_to_load)
        
        # Filter by state if needed
        if states and state_col:
            def norm_state(value):
                if value is None or (isinstance(value, float) and pd.isna(value)):
                    return None
                text = re.sub(r"\s+", " ", str(value).strip())
                lower = text.lower()
                if lower in US_STATES:
                    return US_STATES[lower]
                if text.upper() in STATE_CODES:
                    return text.upper()
                m = re.search(r",\s*([A-Za-z]{2})$", text)
                if m and m.group(1).upper() in STATE_CODES:
                    return m.group(1).upper()
                return None
            
            df["state_norm"] = df[state_col].map(norm_state)
            df = df[df["state_norm"].isin(states)]
        
        # Clean phones
        def clean_phone(value):
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return None
            text = str(value)
            if re.fullmatch(r"\d+\.0", text):
                text = text[:-2]
            digits = re.sub(r"\D", "", text)
            if len(digits) < 10:
                return None
            return digits[-10:]
        
        phones = df[phone_col].map(clean_phone).dropna().unique()
        
        # Match against CRM index
        matched = crm_index.set_index("phone10").reindex(phones).dropna(how="all")
        if matched.empty:
            return None, None, "no matches found in CRM"
        
        # Compute metrics
        is_sale = matched["is_sale"].fillna(False).astype(bool)
        is_transfer = matched["is_transfer"].fillna(False).astype(bool)
        is_red_flag = matched["is_red_flag"].fillna(False).astype(bool)
        
        sales = int(is_sale.sum())
        transfers = int(is_transfer.sum())
        red_flags = int(is_red_flag.sum())
        
        summary = {
            "table": table,
            "phone_column": phone_col,
            "state_column": state_col or "(none)",
            "order_rows": len(df),
            "unique_phones": len(phones),
            "matched_in_crm": len(matched),
            "transfers": transfers,
            "sales": sales,
            "red_flags": red_flags,
            "conversion_%": round(sales / transfers * 100, 1) if transfers else None,
        }
        
        # Per-state breakdown
        by_state = pd.DataFrame({
            "state": matched["state"].fillna("Unknown"),
            "transfer": is_transfer.astype(int),
            "sale": is_sale.astype(int),
        }).groupby("state").sum().reset_index()
        by_state.insert(0, "table", table)
        by_state = by_state.sort_values("sale", ascending=False)
        
        return summary, by_state, None
        
    except Exception as e:
        return None, None, str(e)


def to_excel_bytes(sheets):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


# ==================== Main UI Function ====================
def show_vendor_page():
    """Main function to display the vendor comparison page."""
    st.title("Vendor Order Comparison")
    st.caption("Compare vendor order tables against CRM data to identify which lists produce the most sales and transfers.")
    
    # Sidebar
    with st.sidebar:
        st.header("1. Connect to PostgreSQL")
        
        # Get config from secrets or environment
        db_config = get_db_config()
        
        host = st.text_input("Host", value=db_config["HOST"], key="vendor_host")
        port = st.number_input("Port", value=int(db_config["PORT"]), step=1, key="vendor_port")
        user = st.text_input("User", value=db_config["USER"], key="vendor_user")
        password = st.text_input("Password", value=db_config["PASSWORD"], type="password", key="vendor_password")
        schema = st.text_input("Schema", value=db_config["SCHEMA"], key="vendor_schema")
        
        if st.button("Find databases", key="vendor_find_db"):
            try:
                st.session_state["vendor_databases"] = list_databases(host, port, user, password)
                st.session_state["vendor_conn_error"] = None
            except psycopg2.Error as e:
                st.session_state["vendor_databases"] = []
                st.session_state["vendor_conn_error"] = str(e)
        
        if st.session_state.get("vendor_conn_error"):
            st.error(f"Connection failed: {st.session_state['vendor_conn_error']}")
        
        databases = st.session_state.get("vendor_databases", [])
        database = st.selectbox(
            "2. Database",
            options=databases if databases else ["(click 'Find databases' first)"],
            disabled=not databases,
            key="vendor_database"
        )
        
        st.header("3. CRM file")
        crm_upload = st.file_uploader("Upload the CRM CSV", type=["csv"], key="vendor_crm_upload")
        
        # Show CRM info once loaded
        if crm_upload is not None:
            try:
                crm_index, crm_stats, date_ranges = clean_crm(crm_upload.getvalue())
                st.success(f"CRM loaded: {crm_stats['initial_rows']:,} rows, {crm_stats['unique_phones']:,} unique phones")
                st.info(f"Columns detected: {', '.join(['Phone Number', 'Closing Status', 'Opening Status', 'State', 'Pricing List', 'Creation Date', 'Date of Sale'])}")
                
                # Show stats
                with st.expander("CRM Statistics"):
                    col1, col2 = st.columns(2)
                    col1.metric("Sale rows", f"{crm_stats['sale_rows']:,}")
                    col1.metric("Transfer rows", f"{crm_stats['transfer_rows']:,}")
                    col1.metric("Red Flag rows", f"{crm_stats['red_flag_rows']:,}")
                    col2.metric("Unique sale phones", f"{crm_stats['unique_sale_phones']:,}")
                    col2.metric("Unique transfer phones", f"{crm_stats['unique_transfer_phones']:,}")
                    col2.metric("Unknown state %", f"{crm_stats['unknown_state_pct']:.1f}%")
                    st.metric("Monthly sales value", f"${crm_stats['monthly_sales_value']:,.2f}")
                    st.metric("Sales without price", f"{crm_stats['sales_without_price']:,}")
                
                st.session_state["vendor_crm_index"] = crm_index
                st.session_state["vendor_crm_stats"] = crm_stats
                st.session_state["vendor_date_ranges"] = date_ranges
                st.session_state["vendor_state_options"] = sorted(
                    s for s in crm_index["state"].dropna().unique() if s != "Unknown"
                )
            except ValueError as e:
                st.error(str(e))
    
    # Main area
    if not databases:
        st.info("Enter the connection details in the sidebar and click **Find databases**.")
        return
    
    if database.startswith("("):
        return
    
    try:
        conn = connect(host, port, user, password, database)
    except psycopg2.Error as e:
        st.error(f"Could not open database '{database}': {e}")
        return
    
    tables = list_tables(conn, schema)
    if not tables:
        st.warning(f"No tables found in schema '{schema}' of '{database}'.")
        conn.close()
        return
    
    selected_tables = st.multiselect("Vendor tables to compare", options=tables, key="vendor_tables")
    
    # State filter
    if "vendor_state_options" in st.session_state:
        chosen_states = st.multiselect(
            "State filter (leave empty for all states)", 
            options=st.session_state["vendor_state_options"],
            key="vendor_states"
        )
    else:
        chosen_states = []
    
    # Date filters
    if "vendor_date_ranges" in st.session_state:
        with st.expander("Date filters (optional)"):
            creation_min, creation_max = st.session_state["vendor_date_ranges"]["creation_date"]
            sale_min, sale_max = st.session_state["vendor_date_ranges"]["sale_date"]
            
            creation_filter = st.date_input(
                "Creation Date range (for transfers)",
                value=(creation_min, creation_max) if pd.notna(creation_min) and pd.notna(creation_max) else None,
                key="vendor_creation_filter"
            )
            sale_filter = st.date_input(
                "Date of Sale range (for sales)",
                value=(sale_min, sale_max) if pd.notna(sale_min) and pd.notna(sale_max) else None,
                key="vendor_sale_filter"
            )
    else:
        creation_filter = None
        sale_filter = None
    
    run = st.button("Run analysis", type="primary", disabled=not (selected_tables and "vendor_crm_index" in st.session_state), key="vendor_run")
    
    if run:
        states = set(chosen_states) if chosen_states else None
        crm_index = st.session_state["vendor_crm_index"]
        
        # Apply state filter to CRM index
        if states:
            crm_view = crm_index[crm_index["state"].isin(states)]
        else:
            crm_view = crm_index
        
        # Apply date filters if provided
        if creation_filter and len(creation_filter) == 2:
            crm_view = crm_view[
                (crm_view["state"].isin(states if states else crm_view["state"])) |
                (crm_view["is_transfer"] == True)
            ]
        
        summaries, state_parts, skipped = [], [], []
        progress = st.progress(0.0, text="Analysing tables ...")
        
        for i, table in enumerate(selected_tables, 1):
            summary, by_state, reason = analyse_table(conn, schema, table, crm_view, states, None)
            if summary is None:
                skipped.append(f"{table}: {reason}")
            else:
                summaries.append(summary)
                state_parts.append(by_state)
            progress.progress(i / len(selected_tables), text=f"Analysed {i}/{len(selected_tables)}")
        
        progress.empty()
        conn.close()
        
        for msg in skipped:
            st.warning(f"Skipped {msg}")
        
        if not summaries:
            st.error("No selected table had a usable phone column.")
            return
        
        summary_df = pd.DataFrame(summaries).sort_values(["sales", "transfers"], ascending=False)
        state_df = pd.concat(state_parts, ignore_index=True)
        
        st.session_state["vendor_results"] = {"summary": summary_df, "states": state_df}
    
    # Show results
    results = st.session_state.get("vendor_results")
    if results:
        summary_df = results["summary"]
        state_df = results["states"]
        
        # Calculate state ranking from state_df
        state_ranking = state_df.groupby("state").agg({
            "transfer": "sum",
            "sale": "sum"
        }).reset_index()
        state_ranking["conversion_%"] = (state_ranking["sale"] / state_ranking["transfer"] * 100).round(1)
        state_ranking = state_ranking.sort_values("sale", ascending=False)
        
        # Calculate state table pivot
        state_table_pivot = state_df.pivot_table(
            index="state", 
            columns="table", 
            values="sale", 
            fill_value=0,
            aggfunc="sum"
        ).reset_index()
        state_table_pivot = state_table_pivot.sort_values(by=state_table_pivot.columns[1:].tolist(), ascending=False)
        
        # KPI cards
        st.subheader("KPI Cards")
        best = summary_df.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Top table by sales", best["table"], f"{int(best['sales'])} sales")
        c2.metric("Total sales (selected tables)", f"{int(summary_df['sales'].sum()):,}")
        c3.metric("Total transfers (selected tables)", f"{int(summary_df['transfers'].sum()):,}")
        c4.metric("Total red flags (selected tables)", f"{int(summary_df['red_flags'].sum()):,}")
        
        # Ranking table
        st.subheader("Ranking")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)
        
        # Interactive charts with Plotly
        st.subheader("Interactive Charts")
        
        # Tab organization for charts
        chart_tabs = st.tabs(["Table Performance", "State Analysis", "Conversion Rates", "Advanced Visualizations"])
        
        with chart_tabs[0]:
            st.write("**Sales and Transfers per Table (Interactive)**")
            fig = px.bar(summary_df, x="table", y=["sales", "transfers"], 
                         title="Sales vs Transfers by Table",
                         labels={"value": "Count", "variable": "Metric"},
                         barmode="group")
            fig.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig, use_container_width=True)
            
            # Scatter plot: Sales vs Transfers
            st.write("**Sales vs Transfers Scatter Plot**")
            fig_scatter = px.scatter(summary_df, x="transfers", y="sales", 
                                     hover_data=["table", "conversion_%"],
                                     title="Sales vs Transfers Correlation",
                                     size="matched_in_crm",
                                     color="conversion_%",
                                     color_continuous_scale="Viridis")
            fig_scatter.update_layout(xaxis_title="Transfers", yaxis_title="Sales")
            st.plotly_chart(fig_scatter, use_container_width=True)
            
            # Pie chart: Sales distribution by table
            st.write("**Sales Distribution by Table**")
            fig_pie = px.pie(summary_df, values="sales", names="table", 
                              title="Sales Distribution Across Tables")
            st.plotly_chart(fig_pie, use_container_width=True)
        
        with chart_tabs[1]:
            st.write("**State Performance Analysis**")
            
            # State ranking bar chart
            fig_state = px.bar(state_ranking, x="state", y="sale", 
                              title="Total Sales by State",
                              hover_data=["transfer", "conversion_%"])
            fig_state.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_state, use_container_width=True)
            
            # State vs Transfer chart
            fig_state_tt = px.bar(state_ranking, x="state", y=["sale", "transfer"],
                                   title="Sales and Transfers by State",
                                   barmode="group")
            fig_state_tt.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_state_tt, use_container_width=True)
            
            # Top 10 states pie chart
            st.write("**Top 10 States by Sales (Pie Chart)**")
            top_10_states = state_ranking.head(10)
            fig_state_pie = px.pie(top_10_states, values="sale", names="state",
                                    title="Top 10 States Sales Distribution")
            st.plotly_chart(fig_state_pie, use_container_width=True)
        
        with chart_tabs[2]:
            st.write("**Conversion Rate Analysis**")
            
            # Conversion rate by table
            conv_df = summary_df[summary_df["conversion_%"].notna()].copy()
            if not conv_df.empty:
                fig_conv = px.bar(conv_df, x="table", y="conversion_%",
                                  title="Conversion Rate by Table (%)",
                                  color="conversion_%",
                                  color_continuous_scale="RdYlGn")
                fig_conv.update_layout(xaxis_tickangle=-45)
                st.plotly_chart(fig_conv, use_container_width=True)
            
            # Conversion rate by state
            conv_state_df = state_ranking[state_ranking["conversion_%"].notna()].copy()
            if not conv_state_df.empty:
                fig_conv_state = px.bar(conv_state_df, x="state", y="conversion_%",
                                        title="Conversion Rate by State (%)",
                                        color="conversion_%",
                                        color_continuous_scale="RdYlGn")
                fig_conv_state.update_layout(xaxis_tickangle=-45)
                st.plotly_chart(fig_conv_state, use_container_width=True)
            
            # Conversion rate scatter
            st.write("**Conversion Rate vs Volume**")
            if not conv_df.empty:
                fig_conv_scatter = px.scatter(conv_df, x="transfers", y="conversion_%",
                                             hover_data=["table", "sales"],
                                             title="Conversion Rate vs Transfer Volume",
                                             size="sales",
                                             color="sales",
                                             color_continuous_scale="Plasma")
                st.plotly_chart(fig_conv_scatter, use_container_width=True)
        
        with chart_tabs[3]:
            st.write("**Advanced Visualizations**")
            
            # Heatmap: States vs Tables
            st.write("**State-Table Heatmap (Sales)**")
            if 'state_table_pivot' in locals() and not state_table_pivot.empty:
                # Remove the 'state' column for heatmap
                heatmap_data = state_table_pivot.set_index("state")
                fig_heatmap = px.imshow(heatmap_data,
                                       title="Sales Heatmap: States vs Tables",
                                       labels=dict(x="Table", y="State", color="Sales"),
                                       color_continuous_scale="Viridis")
                st.plotly_chart(fig_heatmap, use_container_width=True)
            
            # Red flag analysis
            st.write("**Red Flag Analysis by Table**")
            fig_redflag = px.bar(summary_df, x="table", y="red_flags",
                                  title="Red Flags by Table",
                                  color="red_flags",
                                  color_continuous_scale="Reds")
            fig_redflag.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_redflag, use_container_width=True)
            
            # Match rate analysis
            st.write("**Match Rate Analysis**")
            summary_df["match_rate_%"] = (summary_df["matched_in_crm"] / summary_df["unique_phones"] * 100).round(1)
            fig_match = px.bar(summary_df, x="table", y="match_rate_%",
                               title="CRM Match Rate by Table (%)",
                               color="match_rate_%",
                               color_continuous_scale="Blues")
            fig_match.update_layout(xaxis_tickangle=-45)
            st.plotly_chart(fig_match, use_container_width=True)
            
            # Funnel chart: Tables → Matches → Transfers → Sales
            st.write("**Conversion Funnel**")
            funnel_data = {
                "Stage": ["Total Unique Phones", "CRM Matches", "Transfers", "Sales"],
                "Count": [
                    summary_df["unique_phones"].sum(),
                    summary_df["matched_in_crm"].sum(),
                    summary_df["transfers"].sum(),
                    summary_df["sales"].sum()
                ]
            }
            funnel_df = pd.DataFrame(funnel_data)
            fig_funnel = px.funnel(funnel_df, x="Count", y="Stage",
                                   title="Overall Conversion Funnel")
            st.plotly_chart(fig_funnel, use_container_width=True)
        
        # Original simple bar chart (kept for compatibility)
        st.subheader("Sales and Transfers per Table (Simple)")
        chart_df = summary_df.set_index("table")[["sales", "transfers"]]
        st.bar_chart(chart_df)
        
        # Per-table state breakdown
        st.subheader("Top States per Table")
        for table in summary_df["table"]:
            part = state_df[state_df["table"] == table].head(10)
            with st.expander(f"{table} — top states by sales"):
                # Display data table
                st.dataframe(part[["state", "transfer", "sale"]], use_container_width=True, hide_index=True)
                
                # Add mini chart for this table
                fig_mini = px.bar(part, x="state", y=["sale", "transfer"],
                                 title=f"{table} - Top 10 States Performance",
                                 barmode="group")
                fig_mini.update_layout(height=300, xaxis_tickangle=-45)
                st.plotly_chart(fig_mini, use_container_width=True)
        
        # Advanced Analysis - State Ranking Across All Tables
        st.divider()
        st.subheader("Advanced Analysis: State Ranking Across All Tables")
        
        # Display state ranking (already calculated above)
        col1, col2 = st.columns(2)
        with col1:
            st.write("**Top 10 States by Sales**")
            top_states = state_ranking.head(10)
            st.dataframe(top_states[["state", "sale", "transfer", "conversion_%"]], 
                         use_container_width=True, hide_index=True)
        
        with col2:
            st.write("**State Summary Statistics**")
            st.metric("Total states with data", len(state_ranking))
            if len(state_ranking) > 0:
                top_state = state_ranking.iloc[0]
                st.metric("Top performing state", top_state["state"], f"{int(top_state['sale'])} sales")
                avg_conversion = state_ranking[state_ranking["transfer"] > 0]["conversion_%"].mean()
                st.metric("Average conversion rate", f"{avg_conversion:.1f}%")
        
        # State performance chart
        st.write("**State Performance Chart**")
        state_chart_df = state_ranking.set_index("state")[["sale", "transfer"]].head(15)
        st.bar_chart(state_chart_df)
        
        # Detailed state breakdown with table contributions
        with st.expander("View Detailed State Breakdown (with table contributions)"):
            # Pivot table: states as rows, tables as columns, sales as values
            st.dataframe(state_table_pivot, use_container_width=True, hide_index=True)
        
        # Comprehensive Summary Table
        st.divider()
        st.subheader("Comprehensive Database Summary")
        
        # Create overall summary table
        overall_summary = pd.DataFrame({
            "Metric": [
                "Total Tables Analyzed",
                "Total Order Rows",
                "Total Unique Phones",
                "Total CRM Matches",
                "Total Transfers",
                "Total Sales",
                "Total Red Flags",
                "Overall Conversion Rate (%)",
                "Total States with Sales",
                "Average Sales per Table",
                "Average Transfers per Table"
            ],
            "Value": [
                len(summary_df),
                summary_df["order_rows"].sum(),
                summary_df["unique_phones"].sum(),
                summary_df["matched_in_crm"].sum(),
                summary_df["transfers"].sum(),
                summary_df["sales"].sum(),
                summary_df["red_flags"].sum(),
                round(summary_df["sales"].sum() / summary_df["transfers"].sum() * 100, 1) if summary_df["transfers"].sum() > 0 else 0,
                len(state_ranking),
                round(summary_df["sales"].sum() / len(summary_df), 1),
                round(summary_df["transfers"].sum() / len(summary_df), 1)
            ]
        })
        
        # Display overall summary
        col1, col2 = st.columns(2)
        with col1:
            st.write("**Overall Database Metrics**")
            st.dataframe(overall_summary, use_container_width=True, hide_index=True)
        
        with col2:
            # Top performers summary
            st.write("**Top Performers**")
            top_performers = pd.DataFrame({
                "Category": ["Best Table by Sales", "Best Table by Transfers", "Best Table by Conversion Rate",
                             "Best State by Sales", "Best State by Transfers", "Best State by Conversion Rate"],
                "Name": [
                    summary_df.iloc[0]["table"],
                    summary_df.sort_values("transfers", ascending=False).iloc[0]["table"],
                    summary_df[summary_df["conversion_%"].notna()].sort_values("conversion_%", ascending=False).iloc[0]["table"] if summary_df["conversion_%"].notna().any() else "N/A",
                    state_ranking.iloc[0]["state"],
                    state_ranking.sort_values("transfer", ascending=False).iloc[0]["state"],
                    state_ranking[state_ranking["conversion_%"].notna()].sort_values("conversion_%", ascending=False).iloc[0]["state"] if state_ranking["conversion_%"].notna().any() else "N/A"
                ],
                "Value": [
                    f"{int(summary_df.iloc[0]['sales'])} sales",
                    f"{int(summary_df.sort_values('transfers', ascending=False).iloc[0]['transfers'])} transfers",
                    f"{summary_df[summary_df['conversion_%'].notna()].sort_values('conversion_%', ascending=False).iloc[0]['conversion_%']}%" if summary_df["conversion_%"].notna().any() else "N/A",
                    f"{int(state_ranking.iloc[0]['sale'])} sales",
                    f"{int(state_ranking.sort_values('transfer', ascending=False).iloc[0]['transfer'])} transfers",
                    f"{state_ranking[state_ranking['conversion_%'].notna()].sort_values('conversion_%', ascending=False).iloc[0]['conversion_%']}%" if state_ranking["conversion_%"].notna().any() else "N/A"
                ]
            })
            st.dataframe(top_performers, use_container_width=True, hide_index=True)
        
        # Detailed combined summary table with state breakdown
        st.write("**Complete Analysis Summary (Tables + States)**")
        
        # Create combined view
        combined_summary = []
        for _, row in summary_df.iterrows():
            table_name = row["table"]
            table_states = state_df[state_df["table"] == table_name].sort_values("sale", ascending=False)
            
            for _, state_row in table_states.iterrows():
                combined_summary.append({
                    "Table": table_name,
                    "State": state_row["state"],
                    "Sales": state_row["sale"],
                    "Transfers": state_row["transfer"],
                    "Conversion_%": round(state_row["sale"] / state_row["transfer"] * 100, 1) if state_row["transfer"] > 0 else 0,
                    "Table_Total_Sales": row["sales"],
                    "Table_Total_Transfers": row["transfers"],
                    "Table_Conversion_%": row["conversion_%"],
                    "State_Rank": table_states[table_states["state"] == state_row["state"]].index[0] + 1
                })
        
        if combined_summary:
            combined_df = pd.DataFrame(combined_summary)
            combined_df = combined_df.sort_values(["Sales", "Transfers"], ascending=False)
            st.dataframe(combined_df, use_container_width=True, hide_index=True)
            
            # Summary statistics for the combined view
            st.write(f"Showing {len(combined_df)} table-state combinations")
            st.info(f"Top performing combination: {combined_df.iloc[0]['Table']} in {combined_df.iloc[0]['State']} with {int(combined_df.iloc[0]['Sales'])} sales")
        
        # Download buttons
        st.subheader("Downloads")
        
        # Create comprehensive Excel with multiple sheets
        comprehensive_sheets = {
            "Table Ranking": summary_df,
            "State Ranking": state_ranking,
            "By State by Table": state_df,
            "State Table Pivot": state_table_pivot if 'state_table_pivot' in locals() else pd.DataFrame(),
            "Overall Summary": overall_summary,
            "Top Performers": top_performers,
            "Combined Table-State": combined_df if 'combined_df' in locals() else pd.DataFrame()
        }
        
        xlsx_comprehensive = to_excel_bytes(comprehensive_sheets)
        
        d1, d2, d3 = st.columns(3)
        d1.download_button(
            "Download Full Analysis (Excel)", 
            data=xlsx_comprehensive, 
            file_name="vendor_full_analysis.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="vendor_download_excel_full"
        )
        d2.download_button(
            "Download Table Ranking (CSV)", 
            data=summary_df.to_csv(index=False).encode("utf-8"),
            file_name="vendor_table_ranking.csv", 
            mime="text/csv",
            key="vendor_download_csv_tables"
        )
        d3.download_button(
            "Download State Ranking (CSV)", 
            data=state_ranking.to_csv(index=False).encode("utf-8"),
            file_name="vendor_state_ranking.csv", 
            mime="text/csv",
            key="vendor_download_csv_states"
        )
    else:
        conn.close()


# For standalone running
if __name__ == "__main__":
    st.set_page_config(page_title="Vendor Order Comparison", layout="wide")
    show_vendor_page()
