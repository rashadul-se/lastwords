"""Platform tables (organisations, subscription request pool, API keys) in the shared 'platform' schema."""
from psycopg2.errors import UniqueViolation
from app.domain.errors import Conflict, NotFound
from app.domain.platform import slugify
from app.infrastructure.db import ScopedDb

PLATFORM_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS organizations(
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL, schema_name TEXT UNIQUE,
    plan TEXT NOT NULL DEFAULT 'FREE', status TEXT NOT NULL DEFAULT 'PROVISIONING',
    contact_email TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS subscription_requests(
    id SERIAL PRIMARY KEY, org_name TEXT NOT NULL, contact_name TEXT NOT NULL, contact_email TEXT NOT NULL,
    country TEXT, expected_residents INT, use_case TEXT, plan_requested TEXT NOT NULL DEFAULT 'FREE',
    status TEXT NOT NULL DEFAULT 'PENDING', token_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), reviewed_at TIMESTAMPTZ, reviewed_by TEXT, review_note TEXT,
    org_id INT REFERENCES organizations(id), key_claimed BOOLEAN NOT NULL DEFAULT FALSE);
CREATE UNIQUE INDEX IF NOT EXISTS ux_req_pending_email ON subscription_requests(lower(contact_email))
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS ix_req_status ON subscription_requests(status, created_at);
CREATE TABLE IF NOT EXISTS api_keys(
    id SERIAL PRIMARY KEY, org_id INT NOT NULL REFERENCES organizations(id), prefix TEXT NOT NULL UNIQUE,
    key_hash TEXT NOT NULL, label TEXT, role TEXT NOT NULL DEFAULT 'ADMIN',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), revoked_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ix_keys_org ON api_keys(org_id);
"""

_REQ_COLS = ("id,org_name,contact_name,contact_email,country,expected_residents,use_case,plan_requested,"
             "status,token_hash,created_at,reviewed_at,reviewed_by,review_note,org_id,key_claimed")
_ORG_COLS = "id,name,slug,schema_name,plan,status,contact_email,created_at"
_KEY_COLS = "id,org_id,prefix,label,role,created_at,revoked_at,last_used_at"


def _dicts(cur) -> list[dict]:
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


class PlatformRepository:
    def __init__(self, db: ScopedDb):
        self._conn = db.conn

    def init_schema(self) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(PLATFORM_SCHEMA_SQL)

    # ---- request pool ----
    def create_request(self, org_name, contact_name, email, country, expected_residents, use_case,
                       plan, token_hash) -> int:
        try:
            with self._conn() as c, c.cursor() as cur:
                cur.execute("INSERT INTO subscription_requests(org_name,contact_name,contact_email,country,"
                            "expected_residents,use_case,plan_requested,token_hash) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (org_name, contact_name, email, country, expected_residents, use_case, plan, token_hash))
                return cur.fetchone()[0]
        except UniqueViolation:
            raise Conflict("A request for this email is already pending review")

    def get_request(self, req_id: int) -> dict | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT {_REQ_COLS} FROM subscription_requests WHERE id=%s", (req_id,))
            rows = _dicts(cur)
        return rows[0] if rows else None

    def list_requests(self, status: str | None, limit: int, offset: int) -> list[dict]:
        q, params = f"SELECT {_REQ_COLS} FROM subscription_requests", []
        if status:
            q += " WHERE status=%s"
            params.append(status)
        with self._conn() as c, c.cursor() as cur:
            cur.execute(q + " ORDER BY created_at LIMIT %s OFFSET %s", params + [limit, offset])
            return _dicts(cur)

    def approve_request(self, req_id: int, plan: str, reviewer: str, note: str) -> dict:
        """One transaction: lock the request, create the organisation, mark the request approved."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT org_name,contact_email,status FROM subscription_requests WHERE id=%s FOR UPDATE",
                        (req_id,))
            row = cur.fetchone()
            if not row:
                raise NotFound("Request not found")
            name, email, status = row
            if status != "PENDING":
                raise Conflict(f"Request is already {status}")
            slug = slugify(name)
            cur.execute("INSERT INTO organizations(name,slug,plan,contact_email) VALUES(%s,%s,%s,%s) RETURNING id",
                        (name, slug, plan, email))
            org_id = cur.fetchone()[0]
            schema = f"org_{org_id}_{slug}"
            cur.execute("UPDATE organizations SET schema_name=%s WHERE id=%s", (schema, org_id))
            cur.execute("UPDATE subscription_requests SET status='APPROVED', reviewed_at=now(), reviewed_by=%s, "
                        "review_note=%s, org_id=%s WHERE id=%s", (reviewer, note, org_id, req_id))
        return self.get_org(org_id)

    def reject_request(self, req_id: int, reviewer: str, note: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE subscription_requests SET status='REJECTED', reviewed_at=now(), reviewed_by=%s, "
                        "review_note=%s WHERE id=%s AND status='PENDING'", (reviewer, note, req_id))
            if cur.rowcount:
                return
            cur.execute("SELECT status FROM subscription_requests WHERE id=%s", (req_id,))
            row = cur.fetchone()
        if not row:
            raise NotFound("Request not found")
        raise Conflict(f"Request is already {row[0]}")

    def claim_key_slot(self, req_id: int) -> bool:
        """Atomic one-shot: True only for the first caller on an approved request."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE subscription_requests SET key_claimed=TRUE "
                        "WHERE id=%s AND status='APPROVED' AND NOT key_claimed", (req_id,))
            return cur.rowcount == 1

    def release_key_slot(self, req_id: int) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE subscription_requests SET key_claimed=FALSE WHERE id=%s", (req_id,))

    # ---- organisations ----
    def get_org(self, org_id: int) -> dict | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT {_ORG_COLS} FROM organizations WHERE id=%s", (org_id,))
            rows = _dicts(cur)
        return rows[0] if rows else None

    def list_orgs(self) -> list[dict]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT {_ORG_COLS} FROM organizations ORDER BY id")
            return _dicts(cur)

    def set_org_status(self, org_id: int, status: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE organizations SET status=%s WHERE id=%s", (status, org_id))
            if not cur.rowcount:
                raise NotFound("Organisation not found")

    # ---- API keys ----
    def insert_key(self, org_id: int, prefix: str, key_hash: str, label: str, role: str) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO api_keys(org_id,prefix,key_hash,label,role) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                        (org_id, prefix, key_hash, label, role))
            return cur.fetchone()[0]

    def find_key(self, prefix: str) -> dict | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT k.id AS key_id,k.key_hash,k.role,k.revoked_at,o.id AS org_id,o.name AS org_name,"
                        "o.schema_name,o.plan,o.status AS org_status FROM api_keys k "
                        "JOIN organizations o ON o.id=k.org_id WHERE k.prefix=%s", (prefix,))
            rows = _dicts(cur)
        return rows[0] if rows else None

    def touch_key(self, key_id: int) -> None:
        """Best-effort, at most one write per key per 5 minutes (keeps the hot path read-only)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE api_keys SET last_used_at=now() WHERE id=%s AND "
                        "(last_used_at IS NULL OR last_used_at < now() - interval '5 minutes')", (key_id,))

    def list_keys(self, org_id: int) -> list[dict]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(f"SELECT {_KEY_COLS} FROM api_keys WHERE org_id=%s ORDER BY id", (org_id,))
            return _dicts(cur)

    def revoke_key(self, key_id: int) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE api_keys SET revoked_at=now() WHERE id=%s AND revoked_at IS NULL", (key_id,))
            if not cur.rowcount:
                raise NotFound("Key not found or already revoked")
