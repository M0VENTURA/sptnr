"""
Routes package.

Blueprint definition is delegated to sub-packages. This package only
provides documentation of the routing structure.

Route modules are registered individually in ``helpers.app_bootstrap``.
Sub-packages with ``__init__.py`` (e.g. ``routes/queue/``, ``routes/scan_routes/``)
define their own blueprints and are imported individually by the app bootstrap.
"""