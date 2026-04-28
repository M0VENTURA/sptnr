#!/usr/bin/env python3
import sqlite3
import os

# Check both potential database paths
for db_path in ['music.db', '/database/sptnr.db', 'C:\\database\\sptnr.db']:
    if os.path.exists(db_path):
        print(f"\nUsing database: {db_path}")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        
        # First, list all tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [r[0] for r in cur.fetchall()]
        print('Available tables:')
        for table in tables:
            cur.execute(f'SELECT COUNT(*) FROM {table};')
            count = cur.fetchone()[0]
            print(f'  {table}: {count} rows')
  
        print('\n---')
      
        # Check if upcoming_releases exists
        if 'upcoming_releases' in tables:
            # Check 2026 releases
            cur.execute("SELECT COUNT(*) FROM upcoming_releases WHERE release_year=2026;")
            count_2026 = cur.fetchone()[0]
            print(f'Total 2026 releases in DB: {count_2026}')
          
            # Check by source
            cur.execute("SELECT source_name, COUNT(*) FROM upcoming_releases WHERE release_year=2026 GROUP BY source_name;")
            print('\nBreakdown by source:')
            for source, count in cur.fetchall():
                print(f'  {source}: {count}')
        else:
            print('upcoming_releases table does NOT exist - need to create it!')
        
        conn.close()
        break
else:
    print("No database found!")
