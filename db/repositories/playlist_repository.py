from sqlalchemy import text

from db.engine import db_session


def create_session(session_name: str, user: str, total_tracks: int, priority: bool) -> int:
    with db_session() as session:
        result = session.execute(
            text("""
                INSERT INTO playlist_download_sessions
                (session_name, "user", status, total_tracks, priority_queue, created_at, updated_at)
                VALUES (:session_name, :user, 'in_progress', :total_tracks, :priority, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """),
            {"session_name": session_name, "user": user, "total_tracks": total_tracks, "priority": priority},
        )
        return result.scalar()


def get_session(session_id: int):
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT id, session_name, "user", status, total_tracks, completed_tracks,
                       failed_tracks, skipped_tracks, created_at, updated_at, completed_at,
                       estimated_completion, average_retry_count
                FROM playlist_download_sessions WHERE id = :session_id
            """),
            {"session_id": session_id},
        )
        return result.fetchone()

# Add your update_session_status and other query functions here