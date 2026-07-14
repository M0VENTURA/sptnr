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

    @app.template_filter('split_artist_collabs')
    def split_artist_collabs(value):
        """Split collaboration artist strings into individual artist names.
        
        Handles ``feat.``, ``ft.``, ``featuring``, ``w/`` delimiters.
        Used by track_detail.html and artist_detail.html to create
        separate artist links for collaborative tracks.
        """
        import re
        if not value:
            return []
        parts = re.split(
            r'\s+(?:w/|feat\.?|ft\.?|featuring)\s+',
            str(value), flags=re.IGNORECASE,
        )
        cleaned = [p.strip() for p in parts if p and p.strip()]
        return cleaned or [str(value).strip()]

    @app.template_filter('escapejs')
    def escapejs(value):
        """Escape strings for safe embedding in JavaScript contexts.
        
        Escapes quotes, backslashes, newlines, and HTML-special characters
        to prevent XSS when injecting user data into ``<script>`` blocks.
        """
        if value is None:
            return ''
        value = str(value)
        escapes = {
            '\\': '\\\\',
            "'": "\\'",
            '"': '\\"',
            '\n': '\\n',
            '\r': '\\r',
            '\t': '\\t',
            '\b': '\\b',
            '\f': '\\f',
            '<': '\\u003C',
            '>': '\\u003E',
            '&': '\\u0026',
        }
        for char, escape in escapes.items():
            value = value.replace(char, escape)
        return value