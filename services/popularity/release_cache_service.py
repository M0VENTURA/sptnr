def _upsert_releases(artist: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        with db_session() as session:
            for row in rows:
                # Ensure 'rtype' parameter is always mapped from 'release_type'
                params = {
                    "artist": artist,
                    "title": row.get("title"),
                    "rtype": row.get("rtype") or row.get("release_type", "album"),
                    "category": row.get("category", "Album"),
                    "source": row.get("source", "musicbrainz"),
                    "release_id": row.get("release_id"),
                    "year": row.get("year"),
                    "is_promo": bool(row.get("is_promo", False)),
                }
                session.execute(
                    text("""
                        INSERT INTO artist_release_cache
                            (artist, title, release_type, category, source, release_id, year, is_promo, updated_at)
                        VALUES (:artist, :title, :rtype, :category, :source, :release_id, :year, :is_promo, CURRENT_TIMESTAMP)
                        ON CONFLICT (artist, title, source) DO UPDATE SET
                            release_type = EXCLUDED.release_type,
                            category = EXCLUDED.category,
                            release_id = EXCLUDED.release_id,
                            year = EXCLUDED.year,
                            is_promo = EXCLUDED.is_promo,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    params,
                )
    except Exception as exc:
        logger.debug(
            "[RELEASE_CACHE] Upsert failed",
            artist=artist,
            error=str(exc),
        )
