#!/usr/bin/env python3
# Read the file
with open('start.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find start and end markers
start_marker = '        # SINGLE DETECTION – User\'s workflow: Discogs=5★'
end_marker = '        # Z-BANDS (apply to everyone except confirmed 5★ singles)'

start_pos = content.find(start_marker)
end_pos = content.find(end_marker)

if start_pos == -1 or end_pos == -1:
    print('ERROR: Could not find start or end marker')
    print(f'start_pos: {start_pos}, end_pos: {end_pos}')
else:
    # Extract the replacement
    replacement = '''        # SINGLE DETECTION – Centralized in singledetection.py
        # -----------------------------------------------------------------------
        from singledetection import rate_track_single_detection
        
        if verbose:
            logging.info(f"Starting single detection for album: {album_name} ({len(album_tracks)} tracks)")
            print(f"\\n   🎵 Single Detection: {album_name}")
            logging.info(f"🎵 Single Detection: {album_name}")
        
        low_evidence_bumps = []  # Track songs with +1★ bump from single hints
        
        for trk in album_tracks:
            # Delegate all single detection to centralized function
            rate_track_single_detection(
                trk, artist_name, album_ctx, config,
                TITLE_SIM_THRESHOLD, COUNT_SHORT_RELEASE_AS_MATCH,
                use_lastfm_single, verbose
            )
            
            # Collect low-evidence bumps for reporting
            if trk.get("stars") == 2:
                low_evidence_bumps.append(trk.get("title", ""))

'''
    
    # Create the new content
    new_content = content[:start_pos] + replacement + content[end_pos:]
    
    # Write back
    with open('start.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    
    print('✅ Replaced single detection block in start.py')
