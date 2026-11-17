from __future__ import annotations

from urllib.parse import urlsplit


class PostgresDatabase:
    """DB-API adapter for the store's fixed, parameterized SQL statements."""

    def __init__(self, url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise RuntimeError("Install the Heroku dependencies to use DATABASE_URL.") from None
        # Heroku supplies the connection URL; never include it in application logs.
        self.connection = psycopg.connect(
            url,
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=10,
            options="-c statement_timeout=10000",
            **(
                {"sslmode": "require"}
                if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}
                else {}
            ),
        )
        self.transactions = []

    def execute(self, statement: str, params=()):
        return self.connection.execute(statement.replace("?", "%s"), params)

    def executemany(self, statement: str, rows):
        with self.connection.cursor() as cursor:
            cursor.executemany(statement.replace("?", "%s"), rows)

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, *exc):
        return self.transactions.pop().__exit__(*exc)

    def close(self) -> None:
        self.connection.close()
