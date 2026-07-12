"""Playlist download session management.

Manages download-session lifecycle for batch playlist imports from
ListenBrainz and other playlist sources.

Key Functions:
    - start_download_session(): Create a new batch download session with
      name, user, total track count, and priority.
    - get_download_session_status(): Query the progress of an active
      download session, including percentage calculation.

Architecture:
    Delegates all persistence to ``db.repositories.playlist_repository``.
    This service layer handles business logic like progress calculation.
"""

from db.repositories import playlist_repository

def start_download_session(name: str, user: str, total: int, priority: bool):
    return playlist_repository.create_session(name, user, total, priority)

def get_download_session_status(session_id: int):
    data = playlist_repository.get_session(session_id)
    if not data: return None
    
    # Logic to calculate percentage
    total = data[4] # total_tracks
    completed = data[5]
    progress = int((completed / total * 100) if total > 0 else 0)
    
    return {**data, "progress_percent": progress}