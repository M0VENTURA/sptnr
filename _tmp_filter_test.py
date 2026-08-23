import re
pat = re.compile(r'\[POPULARITY\]|\[TRACK_STAGE\]|\[TRACK\]|\[TRACK_RESULT\]|\[ALBUM_STAGE\]|\[FINALISE_STAGE\]|\[LOAD_STAGE\]|\[FULL_SCAN\]|\[SCAN_PIPELINE\]|\[scan_runner\]|\[LIBRARY_SYNC\]|\[SINGLE\]|Navidrome Import|Artist scan|popularity scan|Popularity |popularity_scan|Full library scan|Boot scan|Scan complete|Scan failed|Scan stopped|single detection|Singles Detection|SCAN RESULTS|SINGLE CONF|Distribution:|Navidrome: synced|star ratings|★', re.I)
lines = [
    '[1/3] Processing: "Songs of a Lost World" (3 Tracks)',
    '[2/3] Processing: "The Cure" (2 Tracks)',
    'Popularity Scan - Letter \'D\'',
    '[scan_runner] Pass-1 artist pre-scan: dEMOTIONAL',
    '[TRACK_STAGE] something',
]
for l in lines:
    print(repr(pat.search(l) is not None), '|', l[:80])
