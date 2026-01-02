#!/usr/bin/env python3
"""
SPTNR Dashboard - Web interface for managing SPTNR configuration and bookmarks
"""

import os
import yaml
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from pathlib import Path

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Configuration file path
CONFIG_FILE = Path(__file__).parent / "config" / "config.yaml"


def load_config():
    """Load configuration from config.yaml"""
    if not CONFIG_FILE.exists():
        return {}
    
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f) or {}


def save_config(config):
    """Save configuration to config.yaml"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


@app.route('/')
def index():
    """Main dashboard page"""
    config = load_config()
    bookmarks = config.get('bookmarks', [])
    return render_template('dashboard.html', bookmarks=bookmarks)


@app.route('/api/bookmarks', methods=['GET'])
def get_bookmarks():
    """API endpoint to get all bookmarks"""
    config = load_config()
    bookmarks = config.get('bookmarks', [])
    return jsonify(bookmarks)


@app.route('/api/bookmarks', methods=['POST'])
def add_bookmark():
    """API endpoint to add a new bookmark"""
    data = request.json
    
    if not data or 'name' not in data or 'url' not in data:
        return jsonify({'error': 'Invalid bookmark data'}), 400
    
    config = load_config()
    if 'bookmarks' not in config:
        config['bookmarks'] = []
    
    # Add the new bookmark
    config['bookmarks'].append({
        'name': data['name'],
        'url': data['url']
    })
    
    save_config(config)
    return jsonify({'message': 'Bookmark added successfully'}), 201


@app.route('/api/bookmarks/<int:index>', methods=['DELETE'])
def delete_bookmark(index):
    """API endpoint to delete a bookmark"""
    config = load_config()
    bookmarks = config.get('bookmarks', [])
    
    if index < 0 or index >= len(bookmarks):
        return jsonify({'error': 'Bookmark not found'}), 404
    
    bookmarks.pop(index)
    config['bookmarks'] = bookmarks
    save_config(config)
    
    return jsonify({'message': 'Bookmark deleted successfully'}), 200


@app.route('/api/bookmarks/<int:index>', methods=['PUT'])
def update_bookmark(index):
    """API endpoint to update a bookmark"""
    data = request.json
    
    if not data or 'name' not in data or 'url' not in data:
        return jsonify({'error': 'Invalid bookmark data'}), 400
    
    config = load_config()
    bookmarks = config.get('bookmarks', [])
    
    if index < 0 or index >= len(bookmarks):
        return jsonify({'error': 'Bookmark not found'}), 404
    
    bookmarks[index] = {
        'name': data['name'],
        'url': data['url']
    }
    
    config['bookmarks'] = bookmarks
    save_config(config)
    
    return jsonify({'message': 'Bookmark updated successfully'}), 200


if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    templates_dir = Path(__file__).parent / "templates"
    templates_dir.mkdir(exist_ok=True)
    
    # Run the Flask app
    port = int(os.getenv('DASHBOARD_PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
