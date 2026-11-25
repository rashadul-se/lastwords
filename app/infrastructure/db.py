"""One shared connection pool; every checkout is pinned to a single schema (tenant isolation)."""
import re
from contextlib import contextmanager
from psycopg2 import sql
from psycopg2.pool import ThreadedConnectionPool

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def valid_schema(name: str) -> str:
    if not _SCHEMA_RE.match(name or ""):
        raise ValueError(f"Invalid schema name: {name!r}")
    return name


class Database:
    def __init__(self, dsn: str, minconn: int, maxconn: int):
        self._pool = ThreadedConnectionPool(minconn, maxconn, dsn)

    def scoped(self, schema: str) -> "ScopedDb":
        return ScopedDb(self._pool, valid_schema(schema))

    def create_schema(self, schema: str) -> None:
        with self.scoped("public").conn() as c, c.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(valid_schema(schema))))


class ScopedDb:
    """Hands out connections whose search_path is ONLY this schema, so queries cannot touch other tenants."""

    def __init__(self, pool: ThreadedConnectionPool, schema: str):
        self._pool, self.schema = pool, schema

    @contextmanager
    def conn(self):
        c = self._pool.getconn()
        try:
            with c.cursor() as cur:   # SET LOCAL lasts for this transaction only, so it never leaks
                cur.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema)))
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._pool.putconn(c)
