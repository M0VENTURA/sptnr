# Korean (Hangul) track search matching + upload-art 500 (2026-08-31)

## Symptom

Some tracks weren't being downloaded/completed correctly:

- **Stray Kids Korean tracks** (`일상`, `토끼와 거북이`, `미친 놈 (Ex)`,
  `비행기`, `타`) consistently ended `no_qualifying_result` despite Soulseek
  returning 39-101 candidates.
- **Upload image from file** → "An internal server error occurred"
  (Quart's generic 500 hid the real cause).
- **Postgres "terminating connection because of crash of another server
  process"** during queue processing.
- Old client.log `lastfm_artist_mbid` errors (already fixed in a prior
  round — the columns were removed from the update SQL).

## Root causes + fixes

### 1. Hangul titles failed `_score_result`'s HARD TITLE GATE

Two compounding causes in `services/downloads/download_pipeline_service.py`:

- `re.findall(r"[a-z0-9]+", ...)` drops Hangul entirely → the ASCII
  word-based fallback found nothing (`exp_word_count == 0`,
  `significant == []`) and the bracket-stripped core comparison was never
  attempted.
- Korean candidates frequently carry annotations ("일상 (Korean Ver.)",
  "미친 놈 (Ex)") that drop the raw SequenceMatcher ratio below 0.35 → the
  hard title gate rejected EVERY candidate (score 0).

**Fix:** after the raw `title_score` gate, compare the BRACKET-STRIPPED
**core** titles (`normalize_core_title` keeps Hangul intact) on both sides:
accept on core similarity ≥ 0.6 or substring containment, and as a final
fallback accept when a Hangul/CJK expected title appears verbatim in the
filename.  This lets annotated Korean candidates match while still rejecting
genuinely different tracks.

### 2. `_tokenize_meaningful` dropped short Hangul tokens

`services/queue/queue_scoring.py::_tokenize_meaningful` dropped tokens
shorter than 3 chars — for Hangul/CJK every character is a meaningful
word/particle, so `일상` (2) and `타` (1) produced an empty token list →
score 0 in the completion-service fuzzy matcher too.

**Fix:** keep tokens that contain Hangul/CJK characters regardless of length
(single/dual-char Hangul syllables, CJK ideographs, kana).

### 3. Upload-art 500 hid the real error

`routes/album_routes.py::api_album_upload_art` had no try/except around the
multipart parse / `request.files` access, so any exception surfaced as
Quart's generic "An internal server error occurred".  Added a try/except
that logs the real cause (with exc_info) and returns a JSON error with the
message.

### 4. Postgres "crash of another server process"

This is an infrastructure issue (a Postgres backend was killed — typically
OOM from a heavy query or container memory pressure), NOT a code bug.  The
app already treats "terminating connection" as transient and retries
(`db/utils.py::is_transient_pg_startup_error`).  Recommendation: check the
Postgres container memory limits / `shared_buffers` / `work_mem`.

## Files

- `services/downloads/download_pipeline_service.py` — Hangul title gate
- `services/queue/queue_scoring.py` — CJK tokenizer
- `routes/album_routes.py` — upload-art error surfacing
- `tests/test_slskd_wrong_artist_rejection.py` — Hangul matching tests
