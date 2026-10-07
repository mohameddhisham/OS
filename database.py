import streamlit as st


def get_db_config():
    """Get database configuration from Streamlit secrets."""
    return {
        "HOST": st.secrets["HOST"],
        "PORT": st.secrets["PORT"],
        "USER": st.secrets["USER"],
        "PASSWORD": st.secrets["PASSWORD"],
        "SCHEMA": st.secrets["SCHEMA"]
    }
