"""Shared PostgreSQL constants.

Currently defines the SQL parameter placeholder used by all
raw ``cursor.execute()`` calls throughout the codebase.

PostgreSQL uses ``%s`` style placeholders.
This centralised constant avoids hard-coding the placeholder in
50+ query strings across the application.
"""

SQL_PLACEHOLDER = "%s"