"""CRM Analysis page - KPIs and charts for CRM data."""
import os
from pathlib import Path
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime


CRM_DATA_DIR = Path(__file__).parent / "saved_crm_data"
CRM_DATA_DIR.mkdir(exist_ok=True)


def save_crm_data(df, filename):
    """Save CRM data to disk for analysis."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = CRM_DATA_DIR / f"crm_{timestamp}_{filename}"
    df.to_csv(filepath, index=False)
    # Keep only the most recent file
    for old_file in sorted(CRM_DATA_DIR.glob("crm_*.csv"))[:-1]:
        old_file.unlink()
    return filepath


def load_latest_crm_data():
    """Load the most recent CRM data."""
    files = sorted(CRM_DATA_DIR.glob("crm_*.csv"))
    if not files:
        return None
    return pd.read_csv(files[-1])


def categorize_status(status):
    """Categorize status into Opening or Closing/Sales."""
    if pd.isna(status):
        return "Unknown"
    
    status = str(status).strip().lower()
    
    # Opening statuses (transfers or red flag)
    opening_keywords = ["transfer", "red flag", "redflag"]
    if any(keyword in status for keyword in opening_keywords):
        return "Opening"
    
    # Closing/Sales statuses
    closing_keywords = ["approved", "postadeted", "pending client reply", 
                       "pending bank approval"]
    if any(keyword in status for keyword in closing_keywords):
        return "Sales"
    
    return "Other"


def show_crm_analysis_page():
    """Display the CRM analysis page."""
    st.title("CRM Analysis")
    st.write("Analyze CRM data with KPIs and charts.")
    
    # Load CRM data
    df = load_latest_crm_data()
    
    if df is None:
        st.info("No CRM data available. Upload a CRM file in the Data Tools page first.")
        return
    
    st.success(f"Loaded CRM data with {len(df)} records")
    
    # Show data preview
    with st.expander("View Raw Data", expanded=False):
        st.dataframe(df.head(100), use_container_width=True)
    
    # Add status categorization
    df['Status_Category'] = df.apply(lambda row: categorize_status(row.get('Status', row.get('status', ''))), axis=1)
    
    # Try to find date column
    date_col = None
    for col in df.columns:
        if 'date' in col.lower() or 'time' in col.lower():
            date_col = col
            break
    
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df['Day_of_Week'] = df[date_col].dt.day_name()
        df['Month'] = df[date_col].dt.month_name()
        df['Year'] = df[date_col].dt.year
    
    # Try to find agent column
    agent_col = None
    for col in df.columns:
        if 'agent' in col.lower() or 'rep' in col.lower() or 'user' in col.lower():
            agent_col = col
            break
    
    # Try to find state column
    state_col = None
    for col in df.columns:
        if 'state' in col.lower():
            state_col = col
            break
    
    # KPIs
    st.header("Key Performance Indicators")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        total_records = len(df)
        st.metric("Total Records", f"{total_records:,}")
    
    with col2:
        opening_count = len(df[df['Status_Category'] == 'Opening'])
        st.metric("Opening (Transfers/Red Flag)", f"{opening_count:,}")
    
    with col3:
        sales_count = len(df[df['Status_Category'] == 'Sales'])
        st.metric("Sales (Closing)", f"{sales_count:,}")
    
    with col4:
        conversion_rate = (sales_count / total_records * 100) if total_records > 0 else 0
        st.metric("Conversion Rate", f"{conversion_rate:.1f}%")
    
    # Charts
    st.header("Charts & Analysis")
    
    # Status Category Distribution
    st.subheader("Status Category Distribution")
    status_counts = df['Status_Category'].value_counts()
    fig_status = px.pie(values=status_counts.values, names=status_counts.index, 
                        title="Status Category Distribution")
    st.plotly_chart(fig_status, use_container_width=True)
    
    # By State
    if state_col:
        st.subheader("Records by State")
        state_counts = df[state_col].value_counts().head(20)
        fig_state = px.bar(x=state_counts.index, y=state_counts.values,
                          title="Top 20 States by Record Count")
        st.plotly_chart(fig_state, use_container_width=True)
        
        # Sales by State
        st.subheader("Sales by State")
        sales_by_state = df[df['Status_Category'] == 'Sales'][state_col].value_counts().head(20)
        fig_sales_state = px.bar(x=sales_by_state.index, y=sales_by_state.values,
                                 title="Top 20 States by Sales Count")
        st.plotly_chart(fig_sales_state, use_container_width=True)
    
    # Top Day of Week
    if date_col:
        st.subheader("Top Day of Week")
        day_counts = df['Day_of_Week'].value_counts()
        fig_day = px.bar(x=day_counts.index, y=day_counts.values,
                        title="Records by Day of Week")
        st.plotly_chart(fig_day, use_container_width=True)
        
        # Top Month
        st.subheader("Top Month")
        month_counts = df['Month'].value_counts()
        fig_month = px.bar(x=month_counts.index, y=month_counts.values,
                          title="Records by Month")
        st.plotly_chart(fig_month, use_container_width=True)
    
    # Top Agent
    if agent_col:
        st.subheader("Top Agents")
        agent_counts = df[agent_col].value_counts().head(15)
        fig_agent = px.bar(x=agent_counts.index, y=agent_counts.values,
                          title="Top 15 Agents by Record Count")
        st.plotly_chart(fig_agent, use_container_width=True)
        
        # Sales by Agent
        st.subheader("Sales by Agent")
        sales_by_agent = df[df['Status_Category'] == 'Sales'][agent_col].value_counts().head(15)
        fig_sales_agent = px.bar(x=sales_by_agent.index, y=sales_by_agent.values,
                                title="Top 15 Agents by Sales Count")
        st.plotly_chart(fig_sales_agent, use_container_width=True)
    
    # Summary Table by State and Status Category
    st.subheader("Summary by State and Status Category")
    if state_col:
        summary = df.groupby([state_col, 'Status_Category']).size().unstack(fill_value=0)
        st.dataframe(summary, use_container_width=True)
    
    if st.button("Clear Saved CRM Data"):
        for file in CRM_DATA_DIR.glob("crm_*.csv"):
            file.unlink()
        st.rerun()
