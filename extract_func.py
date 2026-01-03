#!/usr/bin/env python3
import re

# Read the file
with open('start.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the line number where scan_artist_to_db is defined (nested)
start_line = None
end_line = None
for i, line in enumerate(lines):
    if 'def scan_artist_to_db(' in line and i > 1000:  # Ensure it's the nested one
        start_line = i
    if start_line is not None and i > start_line and line.strip().startswith('# Cache existing track IDs'):
        end_line = i
        break

if start_line is not None and end_line is not None:
    print(f"Found nested function from line {start_line+1} to {end_line}")
    
    # Extract the function (remove 4-space indentation)
    func_lines = lines[start_line:end_line]
    extracted = []
    for line in func_lines:
        if line.startswith('    '):
            extracted.append(line[4:])
        else:
            extracted.append(line)
    
    # Find where scan_library_to_db is defined
    library_start = None
    for i, line in enumerate(lines):
        if line.strip().startswith('def scan_library_to_db('):
            library_start = i
            break
    
    if library_start is not None:
        print(f"Found scan_library_to_db at line {library_start+1}")
        
        # Build new file
        new_lines = lines[:library_start]
        new_lines.extend(extracted)
        new_lines.append('\n\n')
        new_lines.append(lines[library_start])
        new_lines.extend(lines[library_start+1:end_line])
        new_lines.extend(lines[end_line:])
        
        # Write back
        with open('start.py', 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        print("✓ Successfully extracted scan_artist_to_db to module level")
    else:
        print("✗ Could not find scan_library_to_db")
else:
    print(f"✗ Could not find nested function (start={start_line}, end={end_line})")
