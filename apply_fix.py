#!/usr/bin/env python3
"""Direct fix for tag_manager.py"""

tag_manager_path = r"C:\Script\Github\sptnr\helpers\tag_manager.py"

# Read the file
with open(tag_manager_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Scan through and fix the "genre" line (should be around line 53)
fixed = False
for i, line in enumerate(lines):
    if '"genre",' in line and 'Content' in lines[i-1] if i > 0 else False:
        lines[i] = line.replace('"genre",', '"genres",')
        fixed = True
        print(f"Fixed line {i+1}: {line.strip()} -> {lines[i].strip()}")
        break

# Write back
with open(tag_manager_path, 'w', encoding='utf-8') as f:
    f.writelines(lines)

if fixed:
    print("SUCCESS: Fixed 'genre' to 'genres'")
else:
    # Try alternative search
    with open(tag_manager_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if '"genre",' in content and '"work",' in content:
        # Find and replace the specific pattern
        before = '''    # Content
    "genre", "work",'''
        after = '''    # Content
    "genres", "work",'''
        
        if before in content:
            content = content.replace(before, after)
            with open(tag_manager_path, 'w', encoding='utf-8') as f:
                f.write(content)
            print("SUCCESS: Fixed using string replacement")
        else:
            print("ERROR: Could not find the exact pattern")
    else:
        print("ERROR: grep failed, "genre" or "work" not found")
