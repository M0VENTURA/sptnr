#!/usr/bin/env python3
"""Script to remove video detection functions from start.py"""

# Read the file
with open('start.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the start of the video detection code
start_idx = None
for i, line in enumerate(lines):
    if "# --- Discogs call hygiene: global session with retry/backoff ---" in line:
        start_idx = i
        break

# Find the end of the code (before get_suggested_mbid)
end_idx = None
for i, line in enumerate(lines):
    if "def get_suggested_mbid" in line and i > start_idx:
        end_idx = i
        break

if start_idx is not None and end_idx is not None:
    print(f"Found video detection code from line {start_idx + 1} to line {end_idx}")
    print(f"Lines to remove: {end_idx - start_idx} lines")
    
    # Remove the lines (keep one blank line for spacing)
    new_lines = lines[:start_idx] + ["\n"] + lines[end_idx:]
    
    # Write back
    with open('start.py', 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print(f"✅ Removed {end_idx - start_idx} lines from start.py")
    print(f"New file has {len(new_lines)} lines (was {len(lines)} lines)")
else:
    print(f"❌ Could not find video detection code section")
    print(f"start_idx: {start_idx}, end_idx: {end_idx}")
