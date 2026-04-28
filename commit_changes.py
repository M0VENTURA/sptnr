#!/usr/bin/env python3
"""
Git commit script for the queue metadata album split fix
"""
import subprocess
import sys
import os

def run_command(cmd, cwd=None):
    """Run a shell command and return the output"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)

# Assuming the script is in the workspace root
workspace_root = os.path.dirname(os.path.abspath(__file__))

print(f"Workspace root: {workspace_root}")

# Check git status
print("\n=== Checking git status ===")
code, stdout, stderr = run_command("git status", cwd=workspace_root)
print(stdout)
if stderr:
    print(f"Error: {stderr}", file=sys.stderr)

# Check current branch
print("\n=== Checking current branch ===")
code, stdout, stderr = run_command("git branch --show-current", cwd=workspace_root)
current_branch = stdout.strip()
print(f"Current branch: {current_branch}")

# If not on develop, switch to it
if current_branch != "develop":
    print(f"\n=== Switching to develop branch ===")
    code, stdout, stderr = run_command("git checkout develop", cwd=workspace_root)
    if code == 0:
        print("✅ Successfully switched to develop")
    else:
        print(f"⚠️  Could not switch to develop: {stderr}")
        # Try fetching and checking out
        print("\n=== Fetching origin and checking out develop ===")
        code, stdout, stderr = run_command("git fetch origin && git checkout develop", cwd=workspace_root)
        print(stdout)
        if stderr:
            print(f"Error: {stderr}", file=sys.stderr)

# Stage the modified files
print("\n=== Staging modified files ===")
code, stdout, stderr = run_command("git add app.py download_queue_manager.py", cwd=workspace_root)
if code == 0:
    print("✅ Files staged")
else:
    print(f"Error staging files: {stderr}")

# Create the commit
print("\n=== Creating commit ===")
commit_message = """fix(queue): preserve staging file_path for MBID lookups in multi-track imports

- Add music_file_path column to track final music library path separately
- Stop overwriting file_path (used for MBID deduplication) in organize-group
- Prevents multi-track albums from splitting into separate album entries
- Ensures MBID metadata is correctly preserved and matched across all tracks"""

code, stdout, stderr = run_command(
    f'git commit -m "{commit_message}"',
    cwd=workspace_root
)

if code == 0:
    print("✅ Commit successful!")
    print(stdout)
else:
    print(f"Error creating commit: {stderr}")
    sys.exit(1)

print("\n=== Final git log ===")
code, stdout, stderr = run_command("git log --oneline -3", cwd=workspace_root)
print(stdout)
