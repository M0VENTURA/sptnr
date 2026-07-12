"""Typed dictionary definitions for queue items.

Provides type-safe dict shapes used by the download queue system.
All fields are optional (``total=False``) so partial rows from DB
queries are accepted without type errors.
"""

from typing import TypedDict


class QueueItem(TypedDict, total=False):
    id: int

    artist: str
    album: str
    album_artist: str
    title: str
    duration: float

    track_number: str
    disc_number: str
    year: str

    file_path: str
    matched_file_path: str

    status: str
    source: str
    priority: int