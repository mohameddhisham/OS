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


def is_sale(closing_status):
    """Check if a record is a Sale based on Closing Status."""
    if pd.isna(closing_status):
        return False
    
    status = str(closing_status).strip().lower()
    sales_statuses = ["approved", "postdated", "pending bank approval", "pending client reply"]
    return any(s in status for s in sales_statuses)


def is_transfer(opening_status):
    """Check if a record is a Transfer based on Opening Status."""
    if pd.isna(opening_status):
        return False
    
    status = str(opening_status).strip().lower()
    transfer_statuses = ["transferred", "red flag", "redflag"]
    return any(s in status for s in transfer_statuses)


def get_sales_category(closing_status):
    """Get the specific sales category."""
    if pd.isna(closing_status):
        return "Other"
    
    status = str(closing_status).strip().lower()
    
    if "approved" in status:
        return "Approved"
    elif "postdated" in status:
        return "Postdated"
    elif "pending bank approval" in status:
        return "Pending Bank Approval"
    elif "pending client reply" in status:
        return "Pending Client Reply"
    
    return "Other"


def get_transfer_category(opening_status):
    """Get the specific transfer category."""
    if pd.isna(opening_status):
        return "Other"
    
    status = str(opening_status).strip().lower()
    
    if "transferred" in status:
        return "Transferred"
    elif "red flag" in status or "redflag" in status:
        return "Red Flag"
    
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
        st.dataframe(df.head(100), width='stretch')
    
    # Find Opening Status and Closing Status columns
    opening_status_col = None
    closing_status_col = None
    for col in df.columns:
        col_lower = col.lower()
        if 'opening status' in col_lower or 'openingstatus' in col_lower:
            opening_status_col = col
        if 'closing status' in col_lower or 'closingstatus' in col_lower:
            closing_status_col = col
    
    # Try to find Date of Sale column specifically for analysis
    date_col = None
    for col in df.columns:
        col_lower = col.lower()
        if 'date of sale' in col_lower or 'dateofsale' in col_lower:
            date_col = col
            break
    
    # If Date of Sale not found, fall back to any date column
    if not date_col:
        for col in df.columns:
            if 'date' in col.lower() or 'time' in col.lower():
                date_col = col
                break
    
    if date_col:
        # Convert to datetime, treating '0000-00-00' and invalid dates as NaT
        df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
        df['Day_of_Week'] = df[date_col].dt.day_name()
        df['Month'] = df[date_col].dt.month_name()
        df['Year'] = df[date_col].dt.year
        df['Month_Num'] = df[date_col].dt.month
        df['Year_Num'] = df[date_col].dt.year
    
    # Month Filter
    st.header("Date Filter")
    if date_col and not df[date_col].isna().all():
        # Get available months from the data
        valid_dates = df[df[date_col].notna()].copy()
        if len(valid_dates) > 0:
            valid_dates['Year_Month'] = valid_dates[date_col].dt.to_period('M')
            available_months = sorted(valid_dates['Year_Month'].unique())
            
            # Format for display (chronological order)
            month_options = [m.strftime('%B %Y') for m in available_months]
            
            # Add "All Data" option at the top
            month_options.insert(0, "All Data")
            
            selected_month_display = st.selectbox("Select Month", month_options)
            
            # Filter based on selection
            if selected_month_display != "All Data":
                selected_period = pd.to_datetime(selected_month_display).to_period('M')
                df_filtered = df[df[date_col].dt.to_period('M') == selected_period].copy()
                st.info(f"Showing data for {selected_month_display} ({len(df_filtered)} records)")
            else:
                df_filtered = df.copy()
                st.info(f"Showing all data ({len(df_filtered)} records)")
        else:
            df_filtered = df.copy()
            st.warning("No valid dates found in the data. Showing all records.")
    else:
        df_filtered = df.copy()
        st.warning("No date column found. Showing all records.")
    
    # Calculate Sales and Transfers based on business rules (using filtered data)
    df_filtered['Is_Sale'] = df_filtered[closing_status_col].apply(is_sale) if closing_status_col else False
    df_filtered['Is_Transfer'] = df_filtered[opening_status_col].apply(is_transfer) if opening_status_col else False
    df_filtered['Sales_Category'] = df_filtered[closing_status_col].apply(get_sales_category) if closing_status_col else "Other"
    df_filtered['Transfer_Category'] = df_filtered[opening_status_col].apply(get_transfer_category) if opening_status_col else "Other"
    
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
    
    # Main KPIs (using filtered data)
    total_records = len(df_filtered)
    total_sales = len(df_filtered[df_filtered['Is_Sale']])
    total_transfers = len(df_filtered[df_filtered['Is_Transfer']])
    
    # Sales breakdown
    approved_count = len(df_filtered[df_filtered['Sales_Category'] == 'Approved'])
    postdated_count = len(df_filtered[df_filtered['Sales_Category'] == 'Postdated'])
    pending_bank_count = len(df_filtered[df_filtered['Sales_Category'] == 'Pending Bank Approval'])
    pending_client_count = len(df_filtered[df_filtered['Sales_Category'] == 'Pending Client Reply'])
    
    # Transfer breakdown
    transferred_count = len(df_filtered[df_filtered['Transfer_Category'] == 'Transferred'])
    red_flag_count = len(df_filtered[df_filtered['Transfer_Category'] == 'Red Flag'])
    
    # Sales from transferred and red flag records
    sales_from_transferred = len(df_filtered[(df_filtered['Transfer_Category'] == 'Transferred') & (df_filtered['Is_Sale'])])
    sales_from_red_flag = len(df_filtered[(df_filtered['Transfer_Category'] == 'Red Flag') & (df_filtered['Is_Sale'])])
    
    # Rates
    sales_rate = (total_sales / total_records * 100) if total_records > 0 else 0
    transfer_rate = (total_transfers / total_records * 100) if total_records > 0 else 0
    
    # Display main KPIs
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Total Sales", f"{total_sales:,}")
    with col2:
        st.metric("Total Transfers", f"{total_transfers:,}")
    with col3:
        st.metric("Sales Rate", f"{sales_rate:.1f}%")
    with col4:
        st.metric("Transfer Rate", f"{transfer_rate:.1f}%")
    
    # Sales Breakdown
    st.subheader("Sales Breakdown")
    col5, col6, col7, col8 = st.columns(4)
    with col5:
        st.metric("Approved Sales", f"{approved_count:,}")
    with col6:
        st.metric("Postdated Sales", f"{postdated_count:,}")
    with col7:
        st.metric("Pending Bank Approval", f"{pending_bank_count:,}")
    with col8:
        st.metric("Pending Client Reply", f"{pending_client_count:,}")
    
    # Transfer Breakdown
    st.subheader("Transfer Breakdown")
    col9, col10, col11 = st.columns(3)
    with col9:
        st.metric("Transferred", f"{transferred_count:,}")
    with col10:
        st.metric("Red Flags", f"{red_flag_count:,}")
    with col11:
        st.metric("Total Transfers", f"{total_transfers:,}")
    
    # Daily Analysis
    if date_col:
        st.header("Daily Analysis")
        
        # Create daily breakdown using filtered data
        # Only include records with valid dates
        df_filtered_valid = df_filtered[df_filtered[date_col].notna()].copy()
        
        # Add normalized Date column once
        df_filtered_valid['Date'] = df_filtered_valid[date_col].dt.normalize()
        
        # Today's Analysis
        st.subheader("Today's Analysis")
        if len(df_filtered_valid) > 0:
            today = pd.Timestamp.now().normalize()
            today_data = df_filtered_valid[df_filtered_valid['Date'] == today]
            
            if len(today_data) > 0:
                today_sales = len(today_data[today_data['Is_Sale']])
                today_transfers = len(today_data[today_data['Is_Transfer']])
                today_transferred = len(today_data[today_data['Transfer_Category'] == 'Transferred'])
                today_red_flags = len(today_data[today_data['Transfer_Category'] == 'Red Flag'])
                today_approved = len(today_data[today_data['Sales_Category'] == 'Approved'])
                today_postdated = len(today_data[today_data['Sales_Category'] == 'Postdated'])
                today_pending_bank = len(today_data[today_data['Sales_Category'] == 'Pending Bank Approval'])
                today_pending_client = len(today_data[today_data['Sales_Category'] == 'Pending Client Reply'])
                
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Today's Sales", f"{today_sales:,}")
                with col2:
                    st.metric("Today's Transfers", f"{today_transfers:,}")
                with col3:
                    st.metric("Today's Transferred", f"{today_transferred:,}")
                with col4:
                    st.metric("Today's Red Flags", f"{today_red_flags:,}")
                
                col5, col6, col7, col8 = st.columns(4)
                with col5:
                    st.metric("Approved", f"{today_approved:,}")
                with col6:
                    st.metric("Postdated", f"{today_postdated:,}")
                with col7:
                    st.metric("Pending Bank", f"{today_pending_bank:,}")
                with col8:
                    st.metric("Pending Client", f"{today_pending_client:,}")
            else:
                st.info(f"No records for today ({today.strftime('%Y-%m-%d')})")
        else:
            st.warning("No valid date data available for today's analysis.")
        
        # Last Day with Data Analysis
        st.subheader("Last Day with Data")
        if len(df_filtered_valid) > 0:
            last_day = df_filtered_valid['Date'].max()
            last_day_data = df_filtered_valid[df_filtered_valid['Date'] == last_day]
            
            if len(last_day_data) > 0:
                last_day_sales = len(last_day_data[last_day_data['Is_Sale']])
                last_day_transfers = len(last_day_data[last_day_data['Is_Transfer']])
                last_day_transferred = len(last_day_data[last_day_data['Transfer_Category'] == 'Transferred'])
                last_day_red_flags = len(last_day_data[last_day_data['Transfer_Category'] == 'Red Flag'])
                last_day_approved = len(last_day_data[last_day_data['Sales_Category'] == 'Approved'])
                last_day_postdated = len(last_day_data[last_day_data['Sales_Category'] == 'Postdated'])
                last_day_pending_bank = len(last_day_data[last_day_data['Sales_Category'] == 'Pending Bank Approval'])
                last_day_pending_client = len(last_day_data[last_day_data['Sales_Category'] == 'Pending Client Reply'])
                
                st.info(f"Last day with data: {last_day.strftime('%Y-%m-%d')}")
                
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Sales", f"{last_day_sales:,}")
                with col2:
                    st.metric("Transfers", f"{last_day_transfers:,}")
                with col3:
                    st.metric("Transferred", f"{last_day_transferred:,}")
                with col4:
                    st.metric("Red Flags", f"{last_day_red_flags:,}")
                
                col5, col6, col7, col8 = st.columns(4)
                with col5:
                    st.metric("Approved", f"{last_day_approved:,}")
                with col6:
                    st.metric("Postdated", f"{last_day_postdated:,}")
                with col7:
                    st.metric("Pending Bank", f"{last_day_pending_bank:,}")
                with col8:
                    st.metric("Pending Client", f"{last_day_pending_client:,}")
        else:
            st.warning("No valid date data available for last day analysis.")
        
        # Calculate daily metrics for trends
        if len(df_filtered_valid) > 0:
            # Calculate daily metrics
            daily_transfers = df_filtered_valid[df_filtered_valid['Is_Transfer']].groupby('Date').size().reset_index(name='Total Transfers')
            daily_sales = df_filtered_valid[df_filtered_valid['Is_Sale']].groupby('Date').size().reset_index(name='Total Sales')
            daily_transferred = df_filtered_valid[df_filtered_valid['Transfer_Category'] == 'Transferred'].groupby('Date').size().reset_index(name='Transferred')
            daily_red_flag = df_filtered_valid[df_filtered_valid['Transfer_Category'] == 'Red Flag'].groupby('Date').size().reset_index(name='Red Flags')
            
            daily_approved = df_filtered_valid[df_filtered_valid['Sales_Category'] == 'Approved'].groupby('Date').size().reset_index(name='Approved')
            daily_postdated = df_filtered_valid[df_filtered_valid['Sales_Category'] == 'Postdated'].groupby('Date').size().reset_index(name='Postdated')
            daily_pending_bank = df_filtered_valid[df_filtered_valid['Sales_Category'] == 'Pending Bank Approval'].groupby('Date').size().reset_index(name='Pending Bank Approval')
            daily_pending_client = df_filtered_valid[df_filtered_valid['Sales_Category'] == 'Pending Client Reply'].groupby('Date').size().reset_index(name='Pending Client Reply')
            
            # Merge all daily data
            daily_data = pd.DataFrame({'Date': df_filtered_valid['Date'].unique()})
            daily_data = daily_data.merge(daily_transfers, on='Date', how='left')
            daily_data = daily_data.merge(daily_sales, on='Date', how='left')
            daily_data = daily_data.merge(daily_transferred, on='Date', how='left')
            daily_data = daily_data.merge(daily_red_flag, on='Date', how='left')
            daily_data = daily_data.merge(daily_approved, on='Date', how='left')
            daily_data = daily_data.merge(daily_postdated, on='Date', how='left')
            daily_data = daily_data.merge(daily_pending_bank, on='Date', how='left')
            daily_data = daily_data.merge(daily_pending_client, on='Date', how='left')
            
            # Fill NaN with 0 for all columns except Date
            for col in daily_data.columns:
                if col != 'Date':
                    if pd.api.types.is_numeric_dtype(daily_data[col]):
                        daily_data[col] = daily_data[col].fillna(0)
        else:
            daily_data = pd.DataFrame()
        
        # Daily Trends
        st.subheader("Daily Trends")
        
        if len(daily_data) > 0:
            # Sales and Transfer Trends
            fig_daily = go.Figure()
            fig_daily.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Total Sales'],
                                           mode='lines+markers', name='Total Sales'))
            fig_daily.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Total Transfers'],
                                           mode='lines+markers', name='Total Transfers'))
            fig_daily.update_layout(title="Daily Sales and Transfer Trends",
                                     xaxis_title="Date", yaxis_title="Count")
            st.plotly_chart(fig_daily, width='stretch', key="daily_sales_transfers_trend")
            
            # Transfer Breakdown Trend
            fig_transfer_trend = go.Figure()
            fig_transfer_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Transferred'],
                                                    mode='lines+markers', name='Transferred'))
            fig_transfer_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Red Flags'],
                                                    mode='lines+markers', name='Red Flags'))
            fig_transfer_trend.update_layout(title="Daily Transfer Breakdown",
                                             xaxis_title="Date", yaxis_title="Count")
            st.plotly_chart(fig_transfer_trend, width='stretch', key="daily_transfer_breakdown_trend")
            
            # Sales Status Breakdown Trend
            fig_sales_trend = go.Figure()
            fig_sales_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Approved'],
                                                  mode='lines+markers', name='Approved'))
            fig_sales_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Postdated'],
                                                  mode='lines+markers', name='Postdated'))
            fig_sales_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Pending Bank Approval'],
                                                  mode='lines+markers', name='Pending Bank Approval'))
            fig_sales_trend.add_trace(go.Scatter(x=daily_data['Date'], y=daily_data['Pending Client Reply'],
                                                  mode='lines+markers', name='Pending Client Reply'))
            fig_sales_trend.update_layout(title="Daily Sales Status Breakdown",
                                          xaxis_title="Date", yaxis_title="Count")
            st.plotly_chart(fig_sales_trend, width='stretch', key="daily_sales_status_trend")
            
            # Daily Summary Table
            st.subheader("Daily Summary Table")
            # Date is already datetime, just sort
            st.dataframe(daily_data.sort_values('Date', ascending=False), width='stretch')
        else:
            st.warning("No valid date data available for daily trends.")
    
    # Charts
    st.header("Charts & Analysis")
    
    # Sales and Transfer Distribution
    st.subheader("Sales and Transfer Distribution")
    main_counts = pd.DataFrame({
        'Category': ['Total Sales', 'Total Transfers'],
        'Count': [total_sales, total_transfers]
    })
    fig_main = px.pie(main_counts, values='Count', names='Category',
                      title="Sales vs Transfers Distribution")
    st.plotly_chart(fig_main, width='stretch', key="sales_vs_transfers_pie")
    
    # Sales Breakdown Chart
    st.subheader("Sales Breakdown by Status")
    sales_breakdown = pd.DataFrame({
        'Status': ['Approved', 'Postdated', 'Pending Bank Approval', 'Pending Client Reply'],
        'Count': [approved_count, postdated_count, pending_bank_count, pending_client_count]
    })
    fig_sales_breakdown = px.bar(sales_breakdown, x='Status', y='Count',
                                 title="Sales Breakdown by Status")
    st.plotly_chart(fig_sales_breakdown, width='stretch', key="sales_breakdown_bar")
    
    # Transfer Breakdown Chart
    st.subheader("Transfer Breakdown by Status")
    transfer_breakdown = pd.DataFrame({
        'Status': ['Transferred', 'Red Flag'],
        'Count': [transferred_count, red_flag_count]
    })
    fig_transfer_breakdown = px.bar(transfer_breakdown, x='Status', y='Count',
                                     title="Transfer Breakdown by Status")
    st.plotly_chart(fig_transfer_breakdown, width='stretch', key="transfer_breakdown_bar")
    
    # By State
    if state_col:
        st.subheader("Records by State")
        state_counts = df_filtered[state_col].value_counts().head(20).reset_index()
        state_counts.columns = ['State', 'Count']
        fig_state = px.bar(state_counts, x='State', y='Count',
                          title="Top 20 States by Record Count")
        st.plotly_chart(fig_state, width='stretch', key="states_bar")
        
        # Sales by State
        st.subheader("Sales by State")
        sales_by_state = df_filtered[df_filtered['Is_Sale']][state_col].value_counts().head(20).reset_index()
        sales_by_state.columns = ['State', 'Count']
        fig_sales_state = px.bar(sales_by_state, x='State', y='Count',
                                 title="Top 20 States by Sales Count")
        st.plotly_chart(fig_sales_state, width='stretch', key="sales_by_state_bar")
    
    # Top Day of Week
    if date_col:
        st.subheader("Top Day of Week")
        day_counts = df_filtered['Day_of_Week'].value_counts().reset_index()
        day_counts.columns = ['Day', 'Count']
        fig_day = px.bar(day_counts, x='Day', y='Count',
                        title="Records by Day of Week")
        st.plotly_chart(fig_day, width='stretch', key="day_of_week_bar")
        
        # Top Month
        st.subheader("Top Month")
        month_counts = df_filtered['Month'].value_counts().reset_index()
        month_counts.columns = ['Month', 'Count']
        fig_month = px.bar(month_counts, x='Month', y='Count',
                          title="Records by Month")
        st.plotly_chart(fig_month, width='stretch', key="month_bar")
    
    # Top Agent
    if agent_col:
        st.subheader("Top Agents")
        agent_counts = df_filtered[agent_col].value_counts().head(15).reset_index()
        agent_counts.columns = ['Agent', 'Count']
        fig_agent = px.bar(agent_counts, x='Agent', y='Count',
                          title="Top 15 Agents by Record Count")
        st.plotly_chart(fig_agent, width='stretch', key="agents_bar")
        
        # Sales by Agent
        st.subheader("Sales by Agent")
        sales_by_agent = df_filtered[df_filtered['Is_Sale']][agent_col].value_counts().head(15).reset_index()
        sales_by_agent.columns = ['Agent', 'Count']
        fig_sales_agent = px.bar(sales_by_agent, x='Agent', y='Count',
                                title="Top 15 Agents by Sales Count")
        st.plotly_chart(fig_sales_agent, width='stretch', key="sales_by_agent_bar")
    
    # Conversion Analysis
    st.header("Conversion Analysis")
    
    # Transfer → Sale conversion
    transfer_to_sale = len(df_filtered[df_filtered['Is_Transfer'] & df_filtered['Is_Sale']])
    transfer_conversion_rate = (transfer_to_sale / total_transfers * 100) if total_transfers > 0 else 0
    
    # Transferred → Sale conversion
    transferred_to_sale = len(df_filtered[(df_filtered['Transfer_Category'] == 'Transferred') & df_filtered['Is_Sale']])
    transferred_conversion_rate = (transferred_to_sale / transferred_count * 100) if transferred_count > 0 else 0
    
    # Red Flag → Sale conversion
    red_flag_to_sale = len(df_filtered[(df_filtered['Transfer_Category'] == 'Red Flag') & df_filtered['Is_Sale']])
    red_flag_conversion_rate = (red_flag_to_sale / red_flag_count * 100) if red_flag_count > 0 else 0
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Transfer → Sale", f"{transfer_conversion_rate:.1f}%")
        st.caption(f"{transfer_to_sale} sales from {total_transfers} transfers")
    with col2:
        st.metric("Transferred → Sale", f"{transferred_conversion_rate:.1f}%")
        st.caption(f"{transferred_to_sale} sales from {transferred_count} transferred")
    with col3:
        st.metric("Red Flag → Sale", f"{red_flag_conversion_rate:.1f}%")
        st.caption(f"{red_flag_to_sale} sales from {red_flag_count} red flags")
    
    # Monthly Summary Table
    st.header("Monthly Summary")
    monthly_summary = pd.DataFrame({
        'Metric': ['Total Records', 'Total Sales', 'Total Transfers', 'Transferred', 'Red Flags',
                  'Approved Sales', 'Postdated Sales', 'Pending Bank Approval', 'Pending Client Reply',
                  'Sales Rate', 'Transfer Rate', 'Transfer → Sale Conversion', 'Transferred → Sale Conversion', 'Red Flag → Sale Conversion'],
        'Value': [total_records, total_sales, total_transfers, transferred_count, red_flag_count,
                 approved_count, postdated_count, pending_bank_count, pending_client_count,
                 f"{sales_rate:.1f}%", f"{transfer_rate:.1f}%", f"{transfer_conversion_rate:.1f}%",
                 f"{transferred_conversion_rate:.1f}%", f"{red_flag_conversion_rate:.1f}%"]
    })
    st.dataframe(monthly_summary, width='stretch')
    
    # Summary Table by State and Category
    st.subheader("Summary by State and Category")
    if state_col:
        summary = df_filtered.groupby([state_col, 'Is_Sale', 'Is_Transfer']).size().unstack(fill_value=0)
        st.dataframe(summary, width='stretch')
        monthly_summary = pd.DataFrame({
            'Metric': ['Total Records', 'Total Sales', 'Total Transfers', 'Transferred', 'Red Flags',
                      'Approved Sales', 'Postdated Sales', 'Pending Bank Approval', 'Pending Client Reply',
                      'Sales Rate', 'Transfer Rate', 'Transfer → Sale Conversion', 'Transferred → Sale Conversion', 'Red Flag → Sale Conversion'],
            'Value': [total_records, total_sales, total_transfers, transferred_count, red_flag_count,
                     approved_count, postdated_count, pending_bank_count, pending_client_count,
                     f"{sales_rate:.1f}%", f"{transfer_rate:.1f}%", f"{transfer_conversion_rate:.1f}%",
                     f"{transferred_conversion_rate:.1f}%", f"{red_flag_conversion_rate:.1f}%"]
        })
        st.dataframe(monthly_summary, width='stretch')
    
    # Summary Table by State and Category
    st.subheader("Summary by State and Category")
    if state_col:
        summary = df_filtered.groupby([state_col, 'Is_Sale', 'Is_Transfer']).size().unstack(fill_value=0)
        st.dataframe(summary, width='stretch')
    
    if st.button("Clear Saved CRM Data"):
        for file in CRM_DATA_DIR.glob("crm_*.csv"):
            file.unlink()
        st.rerun()
