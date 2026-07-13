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

    @app.template_filter('regex_replace')
    def regex_replace(value, pattern, replacement):
        """Replace all occurrences of *pattern* with *replacement* in *value*."""
        import re
        if not value:
            return ""
        return re.sub(pattern, replacement, str(value))