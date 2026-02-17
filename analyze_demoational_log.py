#!/usr/bin/env python3
import re

log_file = r'c:\Users\amonso\Downloads\debug_log_20260218_102412.txt'

singles = []
album_tracks = {}

with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# Look for the pattern: "Single detection result - is_single: True"
for i, line in enumerate(lines):
    if 'Single detection result' in line and 'is_single: True' in line:
        # Look backwards to find the [DETECT] line with track info
        for j in range(i-1, max(0, i-100), -1):
            if '[DETECT] Starting single detection' in lines[j]:
                # Extract track info
                match = re.search(r"'([^']+)' by dEMOTIONAL \(album: ([^,]+), pop: ([0-9.]+)\)", lines[j])
                if match:
                    track = match.group(1)
                    album = match.group(2)
                    pop = float(match.group(3))
                    
                    # Extract confidence and sources from result line
                    confidence = 'unknown'
                    sources = []
                    
                    conf_match = re.search(r'confidence: (\w+)', line)
                    if conf_match:
                        confidence = conf_match.group(1)
                    
                    src_match = re.search(r"sources: (\[.*?\])", line)
                    if src_match:
                        sources_str = src_match.group(1)
                        sources = re.findall(r"'([^']+)'", sources_str)
                    
                    if album not in album_tracks:
                        album_tracks[album] = {'singles': [], 'all': []}
                    
                    singles.append({
                        'track': track,
                        'album': album,
                        'popularity': pop,
                        'confidence': confidence,
                        'sources': sources,
                        'is_single': True
                    })
                    album_tracks[album]['singles'].append({
                        'track': track,
                        'pop': pop,
                        'sources': sources,
                        'confidence': confidence
                    })
                    break

print("=" * 80)
print("dEMOTIONAL SINGLES DETECTION SUMMARY")
print("=" * 80)

for album in sorted(album_tracks.keys()):
    print(f"\n📀 Album: {album}")
    print(f"   Singles Detected: {len(album_tracks[album]['singles'])}")
    for single in album_tracks[album]['singles']:
        print(f"     ✓ {single['track']:30s} (pop: {single['pop']:5.1f}) - {', '.join(single['sources'])}")

print(f"\n\nTotal Singles Detected: {len(singles)}")
print("\nAll Singles:")
for single in sorted(singles, key=lambda x: (x['album'], x['track'])):
    print(f"  • {single['track']:35s} [{single['album']}] Pop: {single['popularity']:5.1f} ({single['confidence']}) - {single['sources']}")
