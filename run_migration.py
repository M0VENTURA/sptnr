#!/usr/bin/env python3
"""
Migration script to add music_file_path column to download_queue table
"""
import os
import psycopg2
from psycopg2.extras import RealDictCursor

# Get database connection details from environment variables
db_host = os.environ.get("PG_HOST", "localhost")
db_port = os.environ.get("PG_PORT", "5432")
db_user = os.environ.get("PG_USER", "admin")
db_password = os.environ.get("PG_PASSWORD", "")
db_name = os.environ.get("PG_DB", "sptnr")

try:
    # Connect to the database
    conn = psycopg2.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name
    )
    cursor = conn.cursor()
    
    # Execute the ALTER TABLE command
    sql_add_column = """
        ALTER TABLE download_queue 
        ADD COLUMN IF NOT EXISTS music_file_path VARCHAR(4096) DEFAULT NULL;
    """
    cursor.execute(sql_add_column)
    print("✅ Added music_file_path column to download_queue table")
    
    # Create the index
    sql_create_index = """
        CREATE INDEX IF NOT EXISTS idx_download_queue_music_file_path 
        ON download_queue(music_file_path);
    """
    cursor.execute(sql_create_index)
    print("✅ Created index on music_file_path column")
    
    # Commit the changes
    conn.commit()
    print("✅ Migration completed successfully")
    
    # Close connections
    cursor.close()
    conn.close()
    
except psycopg2.Error as e:
    print(f"❌ Database error: {e}")
    exit(1)
except Exception as e:
    print(f"❌ Error: {e}")
    exit(1)
