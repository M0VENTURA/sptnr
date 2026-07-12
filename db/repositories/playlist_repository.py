from db.context import db_cursor

def create_session(session_name: str, user: str, total_tracks: int, priority: bool) -> int:
    with db_cursor(commit=True) as (_conn, cursor):
        cursor.execute("""
            INSERT INTO playlist_download_sessions 
            (session_name, "user", status, total_tracks, priority_queue, created_at, updated_at)
            VALUES (%s, %s, 'in_progress', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            RETURNING id
        """, (session_name, user, total_tracks, priority))
        return cursor.fetchone()[0]

def get_session(session_id: int):
    with db_cursor() as (_conn, cursor):
        cursor.execute("""
            SELECT id, session_name, "user", status, total_tracks, completed_tracks, 
                   failed_tracks, skipped_tracks, created_at, updated_at, completed_at,
                   estimated_completion, average_retry_count
            FROM playlist_download_sessions WHERE id = %s
        """, (session_id,))
        return cursor.fetchone()

# Add your update_session_status and other query functions here