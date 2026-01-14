import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "database/sptnr.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn
