#!/usr/bin/env python3
"""Add live album filtering to prevent duplicates in artist page display"""
import re

# Read the file
with open('app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find the section where we need to add the filter
# Look for the pattern: albums_by_category["unknown"].append(album_dict)
#                       categorized_albums.add(album_name)
#                       (blank line)
#                        # Process compilation albums

pattern = r'(\s+albums_by_category\["unknown"\]\.append\(album_dict\)\s+categorized_albums\.add\(album_name\)\s+)\n(\s+# Process compilation albums)'

replacement = r'''\1
        # SAFETY: Remove live albums from non-live categories to prevent duplicates
        live_album_names = set(a.get('album', '').lower() for a in albums_by_category.get("live_album", []))
        for cat in ["album", "ep", "single", "unknown"]:
            if live_album_names:
                albums_by_category[cat] = [a for a in albums_by_category[cat] if a.get('album', '').lower() not in live_album_names]

\2'''

content_updated = re.sub(pattern, replacement, content)

# Now add the filter for missing albums
# Look for: else:
#               missing_by_category["album"].append(release_dict)
#           (blank line)
#            # Merge discovered and missing albums by category

pattern2 = r'(\s+else:\s+missing_by_category\["album"\]\.append\(release_dict\)\s+)\n(\s+# Merge discovered and missing albums)'

replacement2 = r'''\1
        # SAFETY: Remove live albums from missing releases in wrong categories
        missing_live_names = set(a.get('title', '').lower() for a in missing_by_category.get("live_album", []))
        for cat in ["album", "ep", "single"]:
            if missing_live_names:
                missing_by_category[cat] = [a for a in missing_by_category[cat] if a.get('title', '').lower() not in missing_live_names]

\2'''

content_updated = re.sub(pattern2, replacement2, content_updated)

# Write the file back
if content_updated != content:
    with open('app.py', 'w', encoding='utf-8') as f:
        f.write(content_updated)
    print("✓ Successfully added live album filters to app.py")
else:
    print("✗ No changes made - patterns not found")
