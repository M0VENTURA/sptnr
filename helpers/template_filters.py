"""Jinja2 template filters for Flask.

Registers custom template filters used by the UI templates.
Current filters:
- ``format_duration`` – Convert seconds to ``M:SS`` display format.

Called once during app factory setup.
"""

def register_filters(app):
    """Register all Jinja2 template filters and context processors."""

    @app.context_processor
    def inject_globals():
        """Inject global template variables."""
        return {
            "dist": "/static/dist",
        }

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

    @app.template_filter('safe_id')
    def safe_id(value):
        """Sanitize a value into a CSS/HTML id-safe token.

        Keeps letters, digits, underscore and hyphen; every other character
        (parentheses, slashes, dots, ampersands, ...) becomes an underscore
        and runs are collapsed — so album names like "MMXX (Hypa Hypa
        Edition)" yield valid selectors instead of breaking
        ``querySelector('#collapse-...')``.
        """
        import re
        if value is None:
            return ""
        cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value))
        return re.sub(r"_+", "_", cleaned).strip("_")

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

    @app.template_filter('path_segment')
    def path_segment(value):
        """Percent-encode a value for use as a single URL path segment.

        Unlike ``|urlencode`` (which turns spaces into ``+`` and is meant for
        query strings), this keeps spaces as ``%20`` and encodes slashes as
        ``%2F`` so names like ``AC/DC`` survive routing as one segment.
        """
        from urllib.parse import quote
        if value is None:
            return ""
        return quote(str(value), safe="")

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

    @app.template_filter('title_case')
    def title_case(value):
        """Display-style title casing without rewriting stored metadata.

        Lowercases every word, then capitalises the first letter of the first
        and last words plus all major words, keeping small function words
        (of, the, and, to, ...) lowercase — "the cost of giving up" becomes
        "The Cost of Giving Up". Used for hero headers only; raw tags are
        never rewritten.
        """
        if not value:
            return ''
        small_words = {
            'a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'from', 'in',
            'into', 'nor', 'of', 'off', 'on', 'or', 'per', 'the', 'to', 'up',
            'vs', 'with',
        }
        words = str(value).split()
        if not words:
            return str(value)
        out = []
        for idx, word in enumerate(words):
            lowered = word.lower()
            if idx == 0 or idx == len(words) - 1 or lowered not in small_words:
                for pos, ch in enumerate(lowered):
                    if ch.isalpha():
                        lowered = lowered[:pos] + ch.upper() + lowered[pos + 1:]
                        break
            out.append(lowered)
        return ' '.join(out)

    # ----------------------------------------------------------------------
    # Tests
    # ----------------------------------------------------------------------
    # ``artist_detail.html`` uses ``selectattr('sources', 'contains', src)``
    # and Jinja has no built-in ``contains`` test, so register one.
    @app.template_test('contains')
    def contains_test(container, item):
        """Return True when *item* is found in *container*.

        Mirrors Python's ``item in container`` (usable as ``is contains``
        or as a ``selectattr`` test).
        """
        if container is None:
            return False
        return item in container