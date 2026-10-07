"""Database viewer page - shows databases, tables and record counts."""
import psycopg2
from database import HOST, PORT, USER, PASSWORD, SCHEMA


def get_databases():
    """Get all database names from the server."""
    try:
        conn = psycopg2.connect(
            host=HOST,
            port=PORT,
            user=USER,
            password=PASSWORD,
            database="postgres"
        )
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
        databases = [row[0] for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return databases
    except Exception as e:
        return [f"Error: {str(e)}"]


def get_table_counts(database_name):
    """Get all table names and their row counts from a specific database."""
    try:
        conn = psycopg2.connect(
            host=HOST,
            port=PORT,
            user=USER,
            password=PASSWORD,
            database=database_name
        )
        cursor = conn.cursor()

        # Get all table names
        cursor.execute(f"""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_schema = '{SCHEMA}'
            ORDER BY table_name
        """)
        tables = [row[0] for row in cursor.fetchall()]

        # Get row count for each table
        table_data = []
        for table in tables:
            cursor.execute(f'SELECT COUNT(*) FROM "{table}"')
            count = cursor.fetchone()[0]
            table_data.append({"Table": table, "Count": count})

        cursor.close()
        conn.close()
        return table_data

    except Exception as e:
        return [{"Table": "Error", "Count": str(e)}]


def show_database_page():
    """Display the database viewer page."""
    import streamlit as st
    import pandas as pd

    st.title("Database Viewer")
    st.write("View all databases, their tables and record counts.")

    if st.button("Refresh", key="refresh_db"):
        st.rerun()

    # Get all databases
    with st.spinner("Getting databases..."):
        databases = get_databases()

    if isinstance(databases, list) and len(databases) > 0 and isinstance(databases[0], str) and databases[0].startswith("Error"):
        st.error(f"Database connection error: {databases[0]}")
        return

    # Select database
    selected_db = st.selectbox("Select Database", databases, key="select_db")

    if selected_db:
        with st.spinner(f"Getting tables from {selected_db}..."):
            table_data = get_table_counts(selected_db)

        if table_data and table_data[0]["Table"] == "Error":
            st.error(f"Error reading tables: {table_data[0]['Count']}")
        else:
            df = pd.DataFrame(table_data)
            st.dataframe(df, use_container_width=True)

            st.write(f"Total tables: {len(table_data)}")
            if "Count" in df.columns:
                st.write(f"Total records: {df['Count'].sum():,}")
