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
    opening_keywords = ["Transferred", "Red Flag", "redflag"]
    if any(keyword in status for keyword in opening_keywords):
        return "Opening"
    
    # Closing/Sales statuses (from Closing Status column)
    closing_keywords = ["approved", "postdated", "pending client reply", 
                       "pending bank approval", "retransfer to client"]
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
    # Try to find Closing Status column first, then Status
    status_col = None
    for col in df.columns:
        if 'closing status' in col.lower() or 'closingstatus' in col.lower():
            status_col = col
            break
    if not status_col:
        status_col = 'Status' if 'Status' in df.columns else ('status' if 'status' in df.columns else df.columns[0])
    
    df['Status_Category'] = df[status_col].apply(categorize_status)
    
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
    status_counts = df['Status_Category'].value_counts().reset_index()
    status_counts.columns = ['Category', 'Count']
    fig_status = px.pie(status_counts, values='Count', names='Category', 
                        title="Status Category Distribution")
    st.plotly_chart(fig_status, use_container_width=True)
    
    # By State
    if state_col:
        st.subheader("Records by State")
        state_counts = df[state_col].value_counts().head(20).reset_index()
        state_counts.columns = ['State', 'Count']
        fig_state = px.bar(state_counts, x='State', y='Count',
                          title="Top 20 States by Record Count")
        st.plotly_chart(fig_state, use_container_width=True)
        
        # Sales by State
        st.subheader("Sales by State")
        sales_by_state = df[df['Status_Category'] == 'Sales'][state_col].value_counts().head(20).reset_index()
        sales_by_state.columns = ['State', 'Count']
        fig_sales_state = px.bar(sales_by_state, x='State', y='Count',
                                 title="Top 20 States by Sales Count")
        st.plotly_chart(fig_sales_state, use_container_width=True)
    
    # Top Day of Week
    if date_col:
        st.subheader("Top Day of Week")
        day_counts = df['Day_of_Week'].value_counts().reset_index()
        day_counts.columns = ['Day', 'Count']
        fig_day = px.bar(day_counts, x='Day', y='Count',
                        title="Records by Day of Week")
        st.plotly_chart(fig_day, use_container_width=True)
        
        # Top Month
        st.subheader("Top Month")
        month_counts = df['Month'].value_counts().reset_index()
        month_counts.columns = ['Month', 'Count']
        fig_month = px.bar(month_counts, x='Month', y='Count',
                          title="Records by Month")
        st.plotly_chart(fig_month, use_container_width=True)
    
    # Top Agent
    if agent_col:
        st.subheader("Top Agents")
        agent_counts = df[agent_col].value_counts().head(15).reset_index()
        agent_counts.columns = ['Agent', 'Count']
        fig_agent = px.bar(agent_counts, x='Agent', y='Count',
                          title="Top 15 Agents by Record Count")
        st.plotly_chart(fig_agent, use_container_width=True)
        
        # Sales by Agent
        st.subheader("Sales by Agent")
        sales_by_agent = df[df['Status_Category'] == 'Sales'][agent_col].value_counts().head(15).reset_index()
        sales_by_agent.columns = ['Agent', 'Count']
        fig_sales_agent = px.bar(sales_by_agent, x='Agent', y='Count',
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
