"""
Database abstraction utilities for SQLite/PostgreSQL compatibility.

Provides helper functions to handle syntax differences between SQLite and PostgreSQL,
including conflict resolution and system table queries.
"""

import sqlite3
from typing import Any, Dict, List, Optional, Union


def normalize_parameter_marker(query: str, is_postgres: bool) -> str:
    """
    Convert parameter markers between SQLite (?) and PostgreSQL (%s).
    
    Args:
        query: SQL query string
        is_postgres: True if target is PostgreSQL
    
    Returns:
        Query with normalized parameter markers
    """
    if is_postgres:
        return query.replace('?', '%s')
    return query


def insert_or_replace(table: str, data: Dict[str, Any], is_postgres: bool = False) -> tuple:
    """
    Generate INSERT OR REPLACE statement for the target database.
    
    Args:
        table: Table name
        data: Dict of column:value pairs
        is_postgres: True if target is PostgreSQL
    
    Returns:
        Tuple of (query, values)
    
    Example:
        query, values = insert_or_replace('tracks', {'id': 123, 'title': 'Song'})
        cursor.execute(query, values)
    """
    cols = list(data.keys())
    vals = list(data.values())
    
    if is_postgres:
        # PostgreSQL: INSERT ... ON CONFLICT DO UPDATE
        cols_str = ', '.join(cols)
        placeholders = ', '.join(['%s'] * len(cols))
        update_str = ', '.join([f"{col} = EXCLUDED.{col}" for col in cols])
        
        # Assuming 'id' is primary key - adjust if needed
        query = f"""
            INSERT INTO {table} ({cols_str})
            VALUES ({placeholders})
            ON CONFLICT (id) DO UPDATE SET {update_str}
        """
    else:
        # SQLite: INSERT OR REPLACE
        cols_str = ', '.join(cols)
        placeholders = ', '.join(['?'] * len(cols))
        query = f"INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({placeholders})"
    
    return query.strip(), vals


def insert_or_ignore(table: str, data: Dict[str, Any], is_postgres: bool = False) -> tuple:
    """
    Generate INSERT OR IGNORE statement for the target database.
    
    Args:
        table: Table name
        data: Dict of column:value pairs
        is_postgres: True if target is PostgreSQL
    
    Returns:
        Tuple of (query, values)
    """
    cols = list(data.keys())
    vals = list(data.values())
    
    if is_postgres:
        # PostgreSQL: INSERT ... ON CONFLICT DO NOTHING
        cols_str = ', '.join(cols)
        placeholders = ', '.join(['%s'] * len(cols))
        query = f"""
            INSERT INTO {table} ({cols_str})
            VALUES ({placeholders})
            ON CONFLICT DO NOTHING
        """
    else:
        # SQLite: INSERT OR IGNORE
        cols_str = ', '.join(cols)
        placeholders = ', '.join(['?'] * len(cols))
        query = f"INSERT OR IGNORE INTO {table} ({cols_str}) VALUES ({placeholders})"
    
    return query.strip(), vals


def table_exists(cursor: Any, table_name: str, is_postgres: bool = False) -> bool:
    """
    Check if a table exists in the database.
    
    Args:
        cursor: Database cursor
        table_name: Name of table to check
        is_postgres: True if using PostgreSQL
    
    Returns:
        True if table exists, False otherwise
    """
    if is_postgres:
        # PostgreSQL: information_schema
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            )
        """, (table_name,))
        result = cursor.fetchone()
        if isinstance(result, dict):
            return result.get('exists', False)
        return bool(result[0]) if result else False
    else:
        # SQLite: sqlite_master
        cursor.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name = ?
        """, (table_name,))
        return cursor.fetchone() is not None


def list_tables(cursor: Any, is_postgres: bool = False) -> List[str]:
    """
    List all tables in the database.
    
    Args:
        cursor: Database cursor
        is_postgres: True if using PostgreSQL
    
    Returns:
        List of table names
    """
    if is_postgres:
        # PostgreSQL: information_schema
        cursor.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public'
        """)
    else:
        # SQLite: sqlite_master
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
        """)
    
    return [row[0] if isinstance(row, tuple) else row['table_name'] for row in cursor.fetchall()]


def get_column_names(cursor: Any, table_name: str, is_postgres: bool = False) -> List[str]:
    """
    Get column names for a table.
    
    Args:
        cursor: Database cursor
        table_name: Name of table
        is_postgres: True if using PostgreSQL
    
    Returns:
        List of column names
    """
    if is_postgres:
        # PostgreSQL: information_schema
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (table_name,))
    else:
        # SQLite: PRAGMA table_info
        cursor.execute(f"PRAGMA table_info({table_name})")
    
    if is_postgres:
        return [row[0] if isinstance(row, tuple) else row['column_name'] for row in cursor.fetchall()]
    else:
        return [row[1] if isinstance(row, tuple) else row['name'] for row in cursor.fetchall()]


def is_postgres_connection(conn: Any) -> bool:
    """
    Detect if connection is PostgreSQL or SQLite.
    
    Args:
        conn: Database connection object
    
    Returns:
        True if PostgreSQL, False if SQLite
    """
    try:
        # Check for psycopg2 connection
        if hasattr(conn, 'isolation_level') and conn.__class__.__name__ == 'connection':
            # psycopg2 connection
            return True
    except:
        pass
    
    # Default to SQLite
    return isinstance(conn, sqlite3.Connection) is False


class DatabaseQuery:
    """
    Query executor that handles SQLite/PostgreSQL compatibility automatically.
    
    Automatically converts parameter markers (? to %s) and detects database type.
    """
    
    def __init__(self, conn: Any):
        """
        Initialize query executor.
        
        Args:
            conn: SQLite or PostgreSQL connection object
        """
        self.conn = conn
        self.is_postgres = is_postgres_connection(conn)
    
    def execute(self, query: str, params: tuple = None) -> Any:
        """
        Execute a query with automatic parameter conversion.
        
        Args:
            query: SQL query (use ? placeholders for both databases)
            params: Tuple of parameter values
        
        Returns:
            Cursor object
        """
        cursor = self.conn.cursor()
        
        # Convert parameter markers if needed
        if self.is_postgres:
            query = normalize_parameter_marker(query, True)
        
        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        return cursor
    
    def execute_and_fetch(self, query: str, params: tuple = None) -> list:
        """
        Execute query and fetch all results.
        
        Args:
            query: SQL query
            params: Parameter tuple
        
        Returns:
            List of rows
        """
        cursor = self.execute(query, params)
        return cursor.fetchall()
    
    def execute_and_fetch_one(self, query: str, params: tuple = None) -> dict:
        """
        Execute query and fetch first result.
        
        Args:
            query: SQL query
            params: Parameter tuple
        
        Returns:
            Single row as dict or None
        """
        cursor = self.execute(query, params)
        return cursor.fetchone()


__all__ = [
    'normalize_parameter_marker',
    'insert_or_replace',
    'insert_or_ignore',
    'table_exists',
    'list_tables',
    'get_column_names',
    'is_postgres_connection',
    'DatabaseQuery',
]
