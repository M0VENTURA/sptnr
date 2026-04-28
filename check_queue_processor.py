#!/usr/bin/env python3
"""
Diagnostic script to check queue processor configuration and queue status.
"""

import os
import sys
import yaml

def check_config():
    """Check slskd configuration"""
    print("=== Configuration Check ===")
    print()
    
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    print(f"Config path: {config_path}")
    
    if not os.path.exists(config_path):
        print(f"❌ Config file not found at {config_path}")
        # Try local path
        config_path = "config/config.yaml"
        if os.path.exists(config_path):
            print(f"✓ Found local config at {config_path}")
        else:
            print("❌ No config file found")
            return None
    else:
        print(f"✓ Config file exists")
    
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        slskd_config = config.get("slskd", {})
        
        print()
        print("slskd Configuration:")
        print(f"  enabled: {slskd_config.get('enabled', False)}")
        print(f"  web_url: {slskd_config.get('web_url', 'NOT SET')}")
        print(f"  api_key: {'SET (****)' if slskd_config.get('api_key') else 'NOT SET'}")
        print()
        
        return slskd_config
        
    except Exception as e:
        print(f"❌ Error reading config: {e}")
        return None

def check_queue():
    """Check download queue status"""
    print("=== Download Queue Status ===")
    print()
    
    try:
        from app import get_db
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Count by status
        cursor.execute("""
            SELECT status, COUNT(*) as count
            FROM download_queue
            GROUP BY status
            ORDER BY count DESC
        """)
        
        rows = cursor.fetchall()
        
        if not rows:
            print("Queue is empty")
        else:
            print("Items by status:")
            for row in rows:
                if isinstance(row, dict):
                    print(f"  {row['status']}: {row['count']}")
                else:
                    print(f"  {row[0]}: {row[1]}")
        
        print()
        
        # Get queued items details
        cursor.execute("""
            SELECT id, artist, title, search_query, status, source, created_at
            FROM download_queue
            WHERE status IN ('queued', 'searching')
            ORDER BY created_at DESC
            LIMIT 5
        """)
        
        items = cursor.fetchall()
        
        if items:
            print(f"Recent queued/searching items ({len(items)}):")
            for item in items:
                if isinstance(item, dict):
                    print(f"  [{item['id']}] {item['status']}: {item['search_query']}")
                else:
                    print(f"  [{item[0]}] {item[3]}: {item[2]}")
        else:
            print("No items in queued or searching status")
        
        conn.close()
        
    except Exception as e:
        print(f"❌ Error checking queue: {e}")
        import traceback
        traceback.print_exc()

def test_slskd_connection(slskd_config):
    """Test connection to slskd"""
    print("=== Testing slskd Connection ===")
    print()
    
    if not slskd_config:
        print("❌ No slskd config available")
        return
    
    if not slskd_config.get('enabled'):
        print("❌ slskd is not enabled in config")
        return
    
    web_url = slskd_config.get('web_url')
    api_key = slskd_config.get('api_key')
    
    if not web_url:
        print("❌ web_url not configured")
        return
    
    if not api_key:
        print("⚠️ WARNING: api_key not set (may cause auth errors)")
    
    try:
        from api_clients.slskd import SlskdClient
        
        client = SlskdClient(web_url, api_key, enabled=True)
        
        print(f"Testing connection to {web_url}...")
        
        # Try to start a test search
        test_query = "test"
        search_id = client.start_search(test_query)
        
        if search_id:
            print(f"✓ Connection successful! Search ID: {search_id}")
        else:
            print("❌ Search failed - check logs for details")
        
    except Exception as e:
        print(f"❌ Connection test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("=" * 60)
    print("Queue Processor Diagnostic Tool")
    print("=" * 60)
    print()
    
    slskd_config = check_config()
    print()
    check_queue()
    print()
    test_slskd_connection(slskd_config)
    print()
    print("=" * 60)
