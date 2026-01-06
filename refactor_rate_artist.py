#!/usr/bin/env python3
"""Script to update rate_artist to use new single detection function"""

with open('start.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

import os
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
# Find the start: "low_evidence_bumps = []"
start_line = None
for i, line in enumerate(lines):
    if 'low_evidence_bumps = []  # Track songs with' in line:
        start_line = i
        break

# Find the line after all the track processing: "# Z-BANDS"
end_line = None
for i in range(start_line + 1, len(lines)):
    if '# Z-BANDS' in lines[i]:
        end_line = i
        break

if start_line is not None and end_line is not None:
    print(f"Found single detection block from line {start_line+1} to {end_line}")
    
    # Create the replacement code
    replacement = '''        # Import single detection function
        from singledetection import rate_track_single_detection
        
        low_evidence_bumps = []  # Track songs with +1★ bump from single hints
        
        for trk in album_tracks:
            # Use centralized single detection logic from singledetection.py
            rate_track_single_detection(
                track=trk,
                artist_name=artist_name,
                album_ctx=album_ctx,
                config=config,
                title_sim_threshold=TITLE_SIM_THRESHOLD,
                count_short_release_as_match=COUNT_SHORT_RELEASE_AS_MATCH,
                use_lastfm_single=use_lastfm_single,
                verbose=verbose
            )
            
            # Collect any low-evidence bumps for reporting
            if trk.get("stars") == 2 and not trk.get("is_single"):
                low_evidence_bumps.append(trk.get("title", ""))

            # ------------------------------------------------------------------
            # Median gate + secondary lookup (kept, but video-only cannot reach here as single)
            # ------------------------------------------------------------------
            if SECONDARY_ENABLED and trk.get("is_single"):
                metric_key = SECONDARY_METRIC if SECONDARY_METRIC in ("score", "spotify") else "score"
                metric_val = float(trk.get("score", 0)) if metric_key == "score" else float(trk.get("spotify_score", 0))
                threshold  = _gate_threshold(metric_key, album_medians, SECONDARY_DELTA)

                has_video_only = (("discogs_video" in trk.get("single_sources", [])) and not ({"discogs", "musicbrainz"} & set(trk.get("single_sources", []))))
                under_median   = (metric_val < threshold)

                if has_video_only and under_median:
                    sec = secondary_single_lookup(
                        trk, artist_name, album_ctx,
                        singles_set=singles_set,
                        required_strong_sources=SECONDARY_REQ_STRONG
                    )
                    logging.debug("Secondary lookup result for '%s': %s", trk.get("title", ""), sec)
                    merged_sources = sorted(set(trk.get("single_sources", [])) | set(sec["sources"]))
                    trk["single_sources"]    = merged_sources
                    trk["single_confidence"] = sec["confidence"]

                        logging.info(
                            f"[median-gate] '{trk.get('title', '')}' {metric_key}={metric_val:.3f} < "
                            f"{album_medians[metric_key]:.3f}-{SECONDARY_DELTA:.3f} "
                            f"→ strategy={MEDIAN_STRATEGY} sources={','.join(trk['single_sources'])}"
                        )

        # '''
    
    new_lines = lines[:start_line] + [replacement + '\n'] + lines[end_line:]
    
    with open('start.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print(f"✅ Updated start.py: Replaced {end_line - start_line} lines with single detection function call")
    print(f"New file has {len(new_lines)} lines (was {len(lines)} lines)")
else:
    print(f"❌ Could not find the block to replace")
    print(f"start_line: {start_line}, end_line: {end_line}")
