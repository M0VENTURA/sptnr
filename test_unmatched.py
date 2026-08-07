import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r'C:\Users\amonso\AppData\Local\Temp\popularr-fix')

tmp = tempfile.mkdtemp()
os.environ['DB_PATH'] = os.path.join(tmp, 'popularr.db')
for k in ('DATABASE_URL', 'PG_DSN', 'PG_HOST', 'PGHOST', 'PG_USER', 'PG_PASSWORD', 'PG_DATABASE'):
    os.environ.pop(k, None)
cfg = os.path.join(tmp, 'config.yaml')
with open(cfg, 'w', encoding='utf-8') as fh:
    fh.write("navidrome:\n  base_url: http://localhost:4533\n  user: test\n  pass: test\n")
os.environ['CONFIG_PATH'] = cfg

from sqlalchemy import text
from db.engine import db_session
import services.downloads.download_folder_service as dfs

dl = Path(tempfile.mkdtemp())
(dl / 'tracked').mkdir()
(dl / 'tracked' / 'a.flac').write_bytes(b'x')
(dl / 'unmatched').mkdir()
(dl / 'unmatched' / '01 Song.flac').write_bytes(b'x')
(dl / 'unmatched' / 'cover.jpg').write_bytes(b'x')
(dl / 'matched').mkdir()
(dl / 'matched' / '01 Done.flac').write_bytes(b'x')
(dl / 'torrents').mkdir()
(dl / '.hidden').mkdir()
(dl / 'loose.flac').write_bytes(b'x')
os.environ['DOWNLOADS_DIR'] = str(dl)

with db_session() as s:
    s.execute(text("CREATE TABLE IF NOT EXISTS musicbrainz_releases (id INTEGER PRIMARY KEY AUTOINCREMENT, release_id TEXT, release_title TEXT, artist TEXT, status TEXT, monitoring_folder_path TEXT, release_year INTEGER, total_tracks INTEGER, discovered_count INTEGER, organized_count INTEGER, finalized_count INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP)"))
    s.execute(text("CREATE TABLE IF NOT EXISTS download_queue (id TEXT PRIMARY KEY, artist TEXT, title TEXT, status TEXT, matched_file_path TEXT, music_file_path TEXT)"))
    s.execute(
        text("INSERT INTO musicbrainz_releases (release_id, release_title, artist, status, monitoring_folder_path, total_tracks, discovered_count) VALUES (:rid, 'T', 'A', 'active', :fp, 1, 1)"),
        {"rid": "r1", "fp": str(dl / 'tracked')},
    )
    s.execute(
        text("INSERT INTO download_queue (id, artist, title, status, matched_file_path, music_file_path) VALUES ('q1', 'B', 'Done', 'imported', :fp, '/music/Done.flac')"),
        {"fp": str(dl / 'matched' / '01 Done.flac')},
    )

result = dfs.get_unmatched_folders()
print('unmatched count:', result.get('count'))
for f in result.get('folders', []):
    print('  ', f['display_name'], '| status:', f['status'], '| audio:', f['audio_count'])
names = {f['display_name']: f for f in result.get('folders', [])}
assert 'unmatched' in names and names['unmatched']['status'] == 'unmatched'
assert 'matched' in names and names['matched']['status'] == 'matched', names.get('matched')
assert 'tracked' not in names, 'tracked folder leaked into unmatched'
assert 'torrents' not in names and '.hidden' not in names
assert 'loose.flac' not in names

r = dfs.delete_download_folder(str(dl / 'unmatched'))
assert r.get('success'), r
assert not (dl / 'unmatched').exists()
r2 = dfs.delete_download_folder(str(dl))
assert not r2.get('success'), r2
r3 = dfs.delete_download_folder(str(Path(tempfile.mkdtemp()) / 'outside'))
assert not r3.get('success'), r3

deleted = dfs.auto_delete_imported_folders()
print('auto-deleted:', deleted)
assert deleted == 1, deleted
assert not (dl / 'matched').exists()
assert (dl / 'tracked').exists()

print('PASS: unmatched listing, matched state, safety rails, auto-delete')
