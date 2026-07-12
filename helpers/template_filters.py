"""Jinja2 template filters for Flask.

Registers custom template filters used by the UI templates.
Current filters:
- ``format_duration`` – Convert seconds to ``M:SS`` display format.

Called once during app factory setup.
"""

def register_filters(app):
    @app.template_filter('format_duration')
    def format_duration(seconds):
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"