"""Apply album identity/original-year fixes to musicbrainz_service.py.

The patch keeps the library album name authoritative and uses the matched
MusicBrainz release-group first-release-date as the album year. A .bak copy is
created. The patched module is syntax-checked before it replaces the original.
"""
from __future__ import annotations

import ast
import re
import shutil
import sys
from pathlib import Path


def sub_one(text: str, pattern: str, replacement: str, label: str, flags: int = 0) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"Could not uniquely patch {label}; matches={count}")
    return updated


def patch(path: Path) -> None:
    original = path.read_text(encoding="utf-8")
    text = original

    # Lock deadlock correction, if still needed.
    text = text.replace("_INIT_LOCK = threading.Lock()", "_INIT_LOCK = threading.RLock()", 1)

    # Repair any chat/editor-corrupted URL helper as a complete unit.
    text = sub_one(
        text,
        r'def _cover_art_url\(release_group_id: str = "", release_id: str = ""\) -> str:\n.*?\n\s*return ""',
        '''def _cover_art_url(release_group_id: str = "", release_id: str = "") -> str:
    if release_group_id:
        return f"https://coverartarchive.org/release-group/{release_group_id}/front-250"
    if release_id:
        return f"https://coverartarchive.org/release/{release_id}/front-250"
    return ""''',
        "cover-art URL helper",
        re.DOTALL,
    )

    # Replace bulk lookup.
    text = sub_one(
        text,
        r'    def lookup_recordings_by_mbid_bulk\(.*?\n(?=    def _recording_to_metadata)',
        '''    def lookup_recordings_by_mbid_bulk(
        self,
        mbids: list[str],
        *,
        album_name: str | None = None,
        original_release_year: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch recordings while preserving album-level identity."""
        if not self.enabled or not mbids:
            logger.info(
                "[MB] bulk recording lookup skipped",
                reason="disabled" if not self.enabled else "no MBIDs",
                authoritative_album_name=album_name,
                original_release_year=original_release_year,
            )
            return {}
        context = {
            "mbid_count": len(mbids),
            "authoritative_album_name": album_name,
            "original_release_year": original_release_year,
        }
        try:
            payload = _call_with_heartbeat(
                "recording.bulk_get",
                self.http.get_recordings_bulk,
                mbids,
                inc="artist-credits+releases+work-rels+genres",
                log_context=context,
            ) or {}
            results: dict[str, dict[str, Any]] = {}
            for recording in payload.get("recordings", []):
                if not isinstance(recording, dict):
                    continue
                mbid = str(recording.get("id") or "").strip()
                if mbid:
                    results[mbid] = self._recording_to_metadata(
                        recording,
                        mbid,
                        1.0,
                        album_name=album_name,
                        original_release_year=original_release_year,
                    )
            logger.info("[MB] bulk recording lookup completed", returned=len(results), **context)
            return results
        except Exception as exc:
            logger.exception("[MB] bulk recording lookup failed", error=_error(exc), **context)
            return {}

''',
        "bulk recording lookup",
        re.DOTALL,
    )

    # Replace converter.
    text = sub_one(
        text,
        r'    def _recording_to_metadata\(.*?\n(?=    def lookup_album_metadata)',
        '''    def _recording_to_metadata(
        self,
        recording: dict[str, Any],
        mbid: str,
        confidence: float,
        *,
        album_name: str | None = None,
        original_release_year: int | None = None,
    ) -> dict[str, Any]:
        """Convert a recording without adopting a specific release identity."""
        credits = recording.get("artist-credit") or []
        first = credits[0] if credits else {}
        artist = str(first.get("name") or "") if isinstance(first, dict) else str(first or "")
        artist_data = (first.get("artist") or {}) if isinstance(first, dict) else {}
        artist_mbid = str(artist_data.get("id") or "") if isinstance(artist_data, dict) else ""
        releases = recording.get("releases") or []
        specific_release = releases[0] if releases and isinstance(releases[0], dict) else {}
        specific_title = str(specific_release.get("title") or "").strip()
        specific_date = str(specific_release.get("date") or "").strip()
        recording_release_year = int(specific_date[:4]) if specific_date[:4].isdigit() else None
        authoritative_album = str(album_name or "").strip()
        effective_album = authoritative_album or specific_title
        effective_year = original_release_year if original_release_year is not None else recording_release_year

        if authoritative_album and specific_title and authoritative_album.casefold() != specific_title.casefold():
            logger.debug(
                "[MB] specific release title ignored in favour of library album name",
                recording_mbid=mbid,
                authoritative_album_name=authoritative_album,
                ignored_release_title=specific_title,
            )
        if original_release_year is not None and recording_release_year not in (None, original_release_year):
            logger.debug(
                "[MB] version year ignored in favour of original release year",
                recording_mbid=mbid,
                ignored_version_year=recording_release_year,
                original_release_year=original_release_year,
            )

        writers: list[str] = []
        work_mbid = ""
        for relation in recording.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            if str(relation.get("type") or "").casefold() not in {"performance", "recording of"}:
                continue
            work = relation.get("work") or {}
            work_mbid = str(work.get("id") or work_mbid)
            for work_relation in work.get("relations") or []:
                if str(work_relation.get("type") or "").casefold() in {"composer", "writer", "lyricist"}:
                    target = work_relation.get("artist") or {}
                    if target.get("name"):
                        writers.append(str(target["name"]))

        genres = [
            str(item.get("name") or "").strip()
            for item in recording.get("genres") or []
            if isinstance(item, dict) and item.get("name")
        ]
        return {
            "title": recording.get("title"),
            "artist": artist,
            "artist_mbid": artist_mbid or None,
            "album": effective_album,
            "album_artist": primary_album_artist(specific_release.get("artist-credit") or []),
            "isrc": _first_isrc(recording),
            "year": effective_year,
            "original_release_year": original_release_year,
            "recording_release_year": recording_release_year,
            "musicbrainz_release_title": specific_title,
            "recording_mbid": mbid,
            "confidence": confidence,
            "writer": ", ".join(dict.fromkeys(writers)),
            "work_mbid": work_mbid,
            "genres": list(dict.fromkeys(genres)),
        }

    def lookup_original_album_year(self, artist: str, album: str) -> int | None:
        """Resolve the matched release-group first-release-date year."""
        context = {"artist": artist, "album": album}
        if not self.enabled or not artist or not album:
            logger.info("[MB] original album year lookup skipped", reason="disabled or incomplete input", **context)
            return None
        clean_album = strip_search_keywords(album)
        query = (
            f'artist:"{escape_lucene_special_chars(artist)}" '
            f'AND releasegroup:"{escape_lucene_special_chars(clean_album)}"'
        )
        started = time.monotonic()
        logger.info("[MB] original album year lookup started", query=query, **context)
        try:
            groups = _call_with_heartbeat(
                "album.original_year_search",
                self.http.search_release_groups,
                query,
                limit=5,
                log_context={**context, "query": query},
            ) or []
            ranked: list[tuple[float, int, dict[str, Any]]] = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                first_release_date = str(group.get("first-release-date") or "").strip()
                if not first_release_date[:4].isdigit():
                    continue
                score = calculate_match_score(
                    str(group.get("title") or ""),
                    group.get("artist-credit") or [],
                    album,
                    artist,
                )
                ranked.append((score, int(first_release_date[:4]), group))
            ranked.sort(key=lambda item: item[0], reverse=True)
            if not ranked or ranked[0][0] < 0.6:
                logger.warning(
                    "[MB] original album year unavailable",
                    candidate_count=len(groups),
                    elapsed_s=round(time.monotonic() - started, 3),
                    **context,
                )
                return None
            score, year, group = ranked[0]
            logger.info(
                "[MB] original album year selected",
                original_release_year=year,
                release_group_mbid=group.get("id"),
                matched_release_group_title=group.get("title"),
                match_score=round(score, 3),
                elapsed_s=round(time.monotonic() - started, 3),
                **context,
            )
            return year
        except Exception as exc:
            logger.exception(
                "[MB] original album year lookup failed",
                error=_error(exc),
                elapsed_s=round(time.monotonic() - started, 3),
                **context,
            )
            return None

''',
        "recording converter and original-year resolver",
        re.DOTALL,
    )

    # Replace album-level lookup.
    text = sub_one(
        text,
        r'    def lookup_album_metadata\(.*?\n(?=    def is_single)',
        '''    def lookup_album_metadata(
        self,
        entries: list[tuple[str, str]],
        candidates_per_entry: int = 5,
        album: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Look up recordings using the library name and original album year."""
        if not self.enabled:
            return {}
        album = str(album or "").strip()
        unique = sorted({(str(t).strip(), str(a).strip()) for t, a in entries or [] if t and a})
        if not unique:
            return {}
        album_artist = unique[0][1]
        original_year = self.lookup_original_album_year(album_artist, album) if album else None
        logger.info(
            "[MB] album metadata authority selected",
            authoritative_album_name=album,
            original_release_year=original_year,
            entry_count=len(unique),
        )
        results: dict[str, dict[str, Any]] = {}
        for chunk_start in range(0, len(unique), _MB_BATCH_CHUNK):
            chunk = unique[chunk_start:chunk_start + _MB_BATCH_CHUNK]
            groups = [
                f'(recording:"{escape_lucene_special_chars(normalize_title_for_lucene_query(title))}" '
                f'AND artist:"{escape_lucene_special_chars(artist)}")'
                for title, artist in chunk
            ]
            context = {
                "chunk_start": chunk_start,
                "chunk_size": len(chunk),
                "authoritative_album_name": album,
                "original_release_year": original_year,
            }
            try:
                recordings = _call_with_heartbeat(
                    "recording.batch_search",
                    self.http.search_recordings,
                    " OR ".join(groups),
                    limit=min(100, len(chunk) * candidates_per_entry),
                    inc="releases+work-rels+genres",
                    log_context=context,
                ) or []
            except Exception as exc:
                logger.exception("[MB] album recording search failed", error=_error(exc), **context)
                continue

            batch: list[tuple[str, str, float]] = []
            for title, artist in chunk:
                normalized = normalize_title_for_mbid_match(title)
                best: dict[str, Any] | None = None
                best_score, best_anchor = 0.0, False
                for recording in recordings:
                    candidate_title = str(recording.get("title") or "")
                    if not edition_annotations_compatible(title, candidate_title):
                        continue
                    score = _mbid_similarity(normalized, normalize_title_for_mbid_match(candidate_title))
                    anchor = _recording_matches_album(recording, album)
                    if score > best_score or (score == best_score and anchor and not best_anchor):
                        best, best_score, best_anchor = recording, score, anchor
                mbid = str((best or {}).get("id") or "")
                if mbid and best_score >= _MB_BATCH_SIMILARITY_FLOOR:
                    key, confidence = self._cache_key(title, artist), round(best_score, 3)
                    batch.append((key, mbid, confidence))
                    with self._mem_lock:
                        self._mbid_cache[key] = (mbid, confidence, time.time())

            if batch:
                metadata = self.lookup_recordings_by_mbid_bulk(
                    [item[1] for item in batch],
                    album_name=album,
                    original_release_year=original_year,
                )
                for key, mbid, confidence in batch:
                    if mbid not in metadata:
                        continue
                    item = {**metadata[mbid], "confidence": confidence}
                    if album:
                        item["album"] = album
                    if original_year is not None:
                        item["year"] = original_year
                        item["original_release_year"] = original_year
                    results[key] = item
                self._save_cache()
            logger.info("[MB] album recording chunk completed", matched_count=len(batch), **context)

        logger.info(
            "[MB] album recording batch completed",
            authoritative_album_name=album,
            original_release_year=original_year,
            entry_count=len(unique),
            matched_count=len(results),
        )
        return results

''',
        "album recording lookup",
        re.DOTALL,
    )

    # Preserve album identity during later merges. Title and artist may still use MB.
    text = sub_one(
        text,
        r'    @staticmethod\n    def merge_metadata\(.*?\n(?=    def get_artist_relationships)',
        '''    @staticmethod
    def merge_metadata(
        base: dict[str, Any],
        mb: dict[str, Any],
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge metadata without replacing the collection album name."""
        overrides = overrides or {}
        def pick(*values: Any) -> Any:
            return next((value for value in values if value not in (None, "")), None)
        return {
            "title": pick(overrides.get("title"), mb.get("title"), base.get("title")),
            "artist": pick(overrides.get("artist"), mb.get("artist"), base.get("artist")),
            "album": pick(overrides.get("album"), base.get("album"), mb.get("album")),
            "album_artist": pick(overrides.get("album_artist"), base.get("album_artist"), mb.get("album_artist")),
            # MB contains the release-group first-release year for album lookups.
            "year": pick(overrides.get("year"), mb.get("original_release_year"), mb.get("year"), base.get("year")),
        }

''',
        "metadata merge",
        re.DOTALL,
    )

    # Syntax-check before writing.
    ast.parse(text, filename=str(path))
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    temp = path.with_suffix(path.suffix + ".new")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)
    print(f"Patched: {path}")
    print(f"Backup:  {backup}")
    print("Syntax:  valid")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python apply_musicbrainz_album_identity_fix.py path/to/musicbrainz_service.py")
    patch(Path(sys.argv[1]).resolve())
