"""PostgreSQL repository: pooled connections, bulk ops, idempotent inserts."""
from datetime import datetime, timezone
import pandas as pd
from psycopg2.extras import execute_values
from app.infrastructure.db import ScopedDb

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SCHEMA = """
CREATE TABLE IF NOT EXISTS residents(
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, age INT, room TEXT, conditions TEXT, diet TEXT);
ALTER TABLE residents ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'IN';
ALTER TABLE residents ADD COLUMN IF NOT EXISTS checkin_at TIMESTAMPTZ;
ALTER TABLE residents ADD COLUMN IF NOT EXISTS checkout_at TIMESTAMPTZ;
ALTER TABLE residents ADD COLUMN IF NOT EXISTS points INT DEFAULT 0;
CREATE TABLE IF NOT EXISTS vitals(
    event_id TEXT PRIMARY KEY, resident_id INT REFERENCES residents(id), ts TIMESTAMPTZ NOT NULL,
    hr INT, temp REAL, spo2 INT, sleep_hrs REAL, steps INT, mood INT, fluids_ml INT);
CREATE INDEX IF NOT EXISTS ix_vitals_res_ts ON vitals(resident_id, ts DESC);
CREATE TABLE IF NOT EXISTS med_log(
    event_id TEXT PRIMARY KEY, resident_id INT REFERENCES residents(id), ts TIMESTAMPTZ NOT NULL,
    drug TEXT, status TEXT);
CREATE INDEX IF NOT EXISTS ix_med_res_ts ON med_log(resident_id, ts DESC);
CREATE TABLE IF NOT EXISTS stays(
    event_id TEXT PRIMARY KEY, resident_id INT REFERENCES residents(id),
    checkin_at TIMESTAMPTZ NOT NULL, checkout_at TIMESTAMPTZ, checkout_event_id TEXT);
CREATE INDEX IF NOT EXISTS ix_stays_res ON stays(resident_id, checkin_at DESC);
CREATE TABLE IF NOT EXISTS ledger(
    event_id TEXT PRIMARY KEY, resident_id INT REFERENCES residents(id), ts TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL, category TEXT, description TEXT, amount NUMERIC(12,2) NOT NULL, receipt_no TEXT);
CREATE INDEX IF NOT EXISTS ix_ledger_ts ON ledger(ts DESC);
CREATE INDEX IF NOT EXISTS ix_ledger_res ON ledger(resident_id, ts DESC);
CREATE TABLE IF NOT EXISTS points_log(
    event_id TEXT PRIMARY KEY, resident_id INT REFERENCES residents(id), ts TIMESTAMPTZ NOT NULL,
    points INT NOT NULL, reason TEXT);
"""

_COLS = {
    "vitals": ("event_id", "resident_id", "ts", "hr", "temp", "spo2", "sleep_hrs", "steps", "mood", "fluids_ml"),
    "med_log": ("event_id", "resident_id", "ts", "drug", "status"),
    "ledger": ("event_id", "resident_id", "ts", "kind", "category", "description", "amount", "receipt_no"),
    "stays": ("event_id", "resident_id", "checkin_at", "checkout_at"),
    "points_log": ("event_id", "resident_id", "ts", "points", "reason"),
}


class PostgresRepository:
    def __init__(self, db: ScopedDb):
        self._db = db
        self._conn = db.conn

    # ---- housekeeping ----
    def init_schema(self) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(SCHEMA)

    def ping(self) -> bool:
        try:
            with self._conn() as c, c.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def counts(self) -> tuple[int, int]:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE status='IN') FROM residents")
            return cur.fetchone()

    def count_residents(self) -> int:
        return self.counts()[0]

    # ---- residents ----
    def insert_residents(self, rows: list[tuple]) -> None:
        with self._conn() as c, c.cursor() as cur:
            execute_values(cur, "INSERT INTO residents(name,age,room,conditions,diet,status,checkin_at,points) "
                                "VALUES %s", rows)

    def add_resident(self, name, age, room, conditions, diet) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO residents(name,age,room,conditions,diet,status,points) "
                        "VALUES(%s,%s,%s,%s,%s,'OUT',0) RETURNING id", (name, age, room, conditions, diet))
            return cur.fetchone()[0]

    def get_resident(self, rid: int) -> dict | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT * FROM residents WHERE id=%s", (rid,))
            row = cur.fetchone()
            return dict(zip([d[0] for d in cur.description], row)) if row else None

    def list_residents(self, search: str, offset: int, limit: int) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(
                "SELECT * FROM residents WHERE name ILIKE %s OR room ILIKE %s ORDER BY id OFFSET %s LIMIT %s",
                c, params=(f"%{search}%", f"%{search}%", offset, limit))

    # ---- bulk + idempotent event application ----
    def bulk_insert(self, table: str, rows: list[dict]) -> None:
        cols = _COLS[table]          # whitelist: table name never comes from user input
        data = [tuple(r.get(c) for c in cols) for r in rows]
        with self._conn() as c, c.cursor() as cur:
            execute_values(cur, f"INSERT INTO {table}({','.join(cols)}) VALUES %s "
                                f"ON CONFLICT (event_id) DO NOTHING", data, page_size=1000)

    def apply_stays(self, rows: list[dict]) -> None:
        with self._conn() as c, c.cursor() as cur:
            for r in rows:
                rid, ts, eid = r["resident_id"], r["ts"], r["event_id"]
                if r["action"] == "IN":
                    cur.execute("SELECT 1 FROM stays WHERE resident_id=%s AND checkout_at IS NULL", (rid,))
                    if cur.fetchone():
                        continue
                    cur.execute("INSERT INTO stays(event_id,resident_id,checkin_at) VALUES(%s,%s,%s) "
                                "ON CONFLICT DO NOTHING", (eid, rid, ts))
                    if cur.rowcount:
                        cur.execute("UPDATE residents SET status='IN', checkin_at=%s, checkout_at=NULL "
                                    "WHERE id=%s", (ts, rid))
                else:
                    cur.execute("SELECT 1 FROM stays WHERE checkout_event_id=%s", (eid,))
                    if cur.fetchone():
                        continue                      # replay guard
                    cur.execute("UPDATE stays SET checkout_at=%s, checkout_event_id=%s WHERE event_id=("
                                "SELECT event_id FROM stays WHERE resident_id=%s AND checkout_at IS NULL "
                                "ORDER BY checkin_at DESC LIMIT 1)", (ts, eid, rid))
                    if cur.rowcount:
                        cur.execute("UPDATE residents SET status='OUT', checkout_at=%s WHERE id=%s", (ts, rid))

    def apply_points(self, rows: list[dict]) -> None:
        with self._conn() as c, c.cursor() as cur:
            for r in rows:
                cur.execute("INSERT INTO points_log(event_id,resident_id,ts,points,reason) "
                            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (r["event_id"], r["resident_id"], r["ts"], r["points"], r["reason"]))
                if cur.rowcount:
                    cur.execute("UPDATE residents SET points=GREATEST(0, COALESCE(points,0)+%s) WHERE id=%s",
                                (r["points"], r["resident_id"]))

    # ---- reads ----
    def vitals_window(self, days: int) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(
                "SELECT resident_id, ts, hr, temp, spo2, sleep_hrs, steps, mood, fluids_ml "
                "FROM vitals WHERE ts > now() - make_interval(days => %s)", c, params=(days,),
                parse_dates=["ts"])

    def resident_history(self, rid: int) -> dict[str, pd.DataFrame]:
        q = {"vitals": "SELECT ts,hr,temp,spo2,sleep_hrs,steps,mood,fluids_ml FROM vitals "
                       "WHERE resident_id=%s ORDER BY ts",
             "meds": "SELECT ts,drug,status FROM med_log WHERE resident_id=%s ORDER BY ts DESC LIMIT 50",
             "stays": "SELECT checkin_at,checkout_at FROM stays WHERE resident_id=%s ORDER BY checkin_at DESC",
             "points": "SELECT ts,points,reason FROM points_log WHERE resident_id=%s ORDER BY ts DESC LIMIT 50"}
        with self._conn() as c:
            return {k: pd.read_sql(sql, c, params=(rid,)) for k, sql in q.items()}

    def recent_stays(self, limit: int) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(
                "SELECT s.resident_id, r.name, s.checkin_at, s.checkout_at FROM stays s "
                "JOIN residents r ON r.id=s.resident_id "
                "ORDER BY GREATEST(s.checkin_at, COALESCE(s.checkout_at, s.checkin_at)) DESC LIMIT %s",
                c, params=(limit,))

    def ledger(self, rid: int | None, start, end) -> pd.DataFrame:
        sql = ("SELECT ts,receipt_no,resident_id,kind,category,description,amount FROM ledger "
               "WHERE ts >= %s AND ts < %s")
        params: list = [start, end]
        if rid:
            sql += " AND resident_id=%s"
            params.append(rid)
        with self._conn() as c:
            df = pd.read_sql(sql + " ORDER BY ts DESC LIMIT 5000", c, params=params, parse_dates=["ts"])
        df["amount"] = df["amount"].astype(float)
        return df

    def finance_summary(self, start, end) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql("SELECT kind, category, SUM(amount) AS total, COUNT(*) AS entries FROM ledger "
                             "WHERE ts >= %s AND ts < %s GROUP BY kind, category ORDER BY kind, total DESC",
                             c, params=(start, end))
        df["total"] = df["total"].astype(float)
        return df

    # ---- finance reporting (aggregated in SQL: never pulls raw rows) ----
    def ledger_totals(self, rid: int | None, start, end, unassigned_only: bool = False) -> pd.DataFrame:
        sql_ = "SELECT kind, category, COALESCE(SUM(amount),0) AS total FROM ledger WHERE ts >= %s AND ts < %s"
        params: list = [start, end]
        if rid:
            sql_ += " AND resident_id=%s"
            params.append(rid)
        elif unassigned_only:
            sql_ += " AND resident_id IS NULL"
        with self._conn() as c:
            df = pd.read_sql(sql_ + " GROUP BY kind, category", c, params=params)
        df["total"] = df["total"].astype(float)
        return df

    def ledger_monthly(self, rid: int | None, start, end) -> pd.DataFrame:
        sql_ = ("SELECT date_trunc('month', ts) AS month, kind, category, SUM(amount) AS total FROM ledger "
                "WHERE ts >= %s AND ts < %s")
        params: list = [start, end]
        if rid:
            sql_ += " AND resident_id=%s"
            params.append(rid)
        with self._conn() as c:
            df = pd.read_sql(sql_ + " GROUP BY 1,2,3 ORDER BY 1", c, params=params, parse_dates=["month"])
        df["total"] = df["total"].astype(float)
        return df

    def guest_days(self, rid: int | None, start, end) -> pd.DataFrame:
        """Days each guest was in house inside [start, end) - the basis for allocating centre costs."""
        sql_ = ("SELECT resident_id, SUM(GREATEST(0, EXTRACT(EPOCH FROM (LEAST(COALESCE(checkout_at, %(e)s), %(e)s) "
                "- GREATEST(checkin_at, %(s)s)))/86400.0)) AS days FROM stays "
                "WHERE checkin_at < %(e)s AND COALESCE(checkout_at, %(e)s) > %(s)s")
        params: dict = {"s": start, "e": end}
        if rid:
            sql_ += " AND resident_id=%(r)s"
            params["r"] = rid
        with self._conn() as c:
            df = pd.read_sql(sql_ + " GROUP BY resident_id", c, params=params)
        df["days"] = df["days"].astype(float)
        return df

    def guest_ledger(self, start, end) -> pd.DataFrame:
        """Per-guest revenue (excl. deposits) and direct costs (excl. refunds)."""
        q = ("SELECT l.resident_id, r.name, "
             "COALESCE(SUM(l.amount) FILTER (WHERE l.kind='INCOME' AND l.category<>'Deposit'),0) AS revenue, "
             "COALESCE(SUM(l.amount) FILTER (WHERE l.kind='EXPENSE' AND l.category<>'Refund'),0) AS direct_cost "
             "FROM ledger l JOIN residents r ON r.id=l.resident_id "
             "WHERE l.ts >= %s AND l.ts < %s GROUP BY l.resident_id, r.name")
        with self._conn() as c:
            df = pd.read_sql(q, c, params=[start, end])
        df[["revenue", "direct_cost"]] = df[["revenue", "direct_cost"]].astype(float)
        return df

    # ---- guest-data signals for recommendations ----
    def diet_counts(self) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql("SELECT diet, COUNT(*) AS n FROM residents WHERE status='IN' "
                               "GROUP BY diet ORDER BY n DESC", c)

    def condition_counts(self) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql("SELECT conditions, COUNT(*) AS n FROM residents WHERE status='IN' "
                               "GROUP BY conditions ORDER BY n DESC", c)

    def med_adherence(self, days: int) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(
                "SELECT resident_id, COUNT(*) AS total, COUNT(*) FILTER (WHERE status<>'GIVEN') AS not_given "
                "FROM med_log WHERE ts > now() - make_interval(days => %s) GROUP BY resident_id",
                c, params=(days,))
