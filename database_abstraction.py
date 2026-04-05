"""Database abstraction utilities for PostgreSQL-only runtime."""

from typing import Any, Dict, List, Optional, Union

try:
    import psycopg2
    import psycopg2.extensions as _psycopg2_ext
except ImportError:
    psycopg2 = None  # type: ignore
    _psycopg2_ext = None  # type: ignore


def normalize_parameter_marker(query: str, is_postgres: bool) -> str:
    """Normalize parameter markers to PostgreSQL style (%s)."""
    return query.replace('?', '%s')


def insert_or_replace(table: str, data: Dict[str, Any], is_postgres: bool = True) -> tuple:
    """
    Generate PostgreSQL upsert statement for the target database.
    
    Args:
        table: Table name
        data: Dict of column:value pairs
        is_postgres: Unused, retained for backward-compatible call sites
    
    Returns:
        Tuple of (query, values)
    
    Example:
        query, values = insert_or_replace('tracks', {'id': 123, 'title': 'Song'})
        cursor.execute(query, values)
    """
    cols = list(data.keys())
    vals = list(data.values())
    
    cols_str = ', '.join(cols)
    placeholders = ', '.join(['%s'] * len(cols))
    update_str = ', '.join([f"{col} = EXCLUDED.{col}" for col in cols])

    query = f"""
        INSERT INTO {table} ({cols_str})
        VALUES ({placeholders})
        ON CONFLICT (id) DO UPDATE SET {update_str}
    """
    
    return query.strip(), vals


def insert_or_ignore(table: str, data: Dict[str, Any], is_postgres: bool = True) -> tuple:
    """
    Generate PostgreSQL INSERT ... ON CONFLICT DO NOTHING statement.
    
    Args:
        table: Table name
        data: Dict of column:value pairs
        is_postgres: Unused, retained for backward-compatible call sites
    
    Returns:
        Tuple of (query, values)
    """
    cols = list(data.keys())
    vals = list(data.values())
    
    cols_str = ', '.join(cols)
    placeholders = ', '.join(['%s'] * len(cols))
    query = f"""
        INSERT INTO {table} ({cols_str})
        VALUES ({placeholders})
        ON CONFLICT DO NOTHING
    """
    
    return query.strip(), vals


def table_exists(cursor: Any, table_name: str, is_postgres: bool = True) -> bool:
    """
    Check if a table exists in the database.
    
    Args:
        cursor: Database cursor
        table_name: Name of table to check
        is_postgres: True if using PostgreSQL
    
    Returns:
        True if table exists, False otherwise
    """
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


def list_tables(cursor: Any, is_postgres: bool = True) -> List[str]:
    """
    List all tables in the database.
    
    Args:
        cursor: Database cursor
        is_postgres: True if using PostgreSQL
    
    Returns:
        List of table names
    """
    cursor.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public'
    """)

    return [row[0] if isinstance(row, tuple) else row['table_name'] for row in cursor.fetchall()]


def get_column_names(cursor: Any, table_name: str, is_postgres: bool = True) -> List[str]:
    """
    Get column names for a table.
    
    Args:
        cursor: Database cursor
        table_name: Name of table
        is_postgres: True if using PostgreSQL
    
    Returns:
        List of column names
    """
    cursor.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table_name,))

    return [row[0] if isinstance(row, tuple) else row['column_name'] for row in cursor.fetchall()]


def is_postgres_connection(conn: Any) -> bool:
    """
    Detect if the connection is a live psycopg2 PostgreSQL connection.

    Handles the _AutoRollbackPGConnection wrapper returned by get_db_connection()
    by unwrapping to the underlying psycopg2 connection before checking.
    
    Args:
        conn: Database connection object
    
    Returns:
        True if PostgreSQL (psycopg2), False otherwise
    """
    try:
        if _psycopg2_ext is None:
            return False
        # Unwrap _AutoRollbackPGConnection (or any single-level wrapper with _conn)
        underlying = getattr(conn, "_conn", conn)
        return isinstance(underlying, _psycopg2_ext.connection)
    except Exception:
        return False


class DatabaseQuery:
    """PostgreSQL query executor with optional placeholder normalization."""
    
    def __init__(self, conn: Any):
        """
        Initialize query executor.

        Args:
            conn: PostgreSQL connection object
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
