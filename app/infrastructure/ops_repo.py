"""Inventory, procurement, HR and user-access persistence (online only: these need transactions)."""
import uuid
import pandas as pd
from psycopg2.errors import UniqueViolation
from psycopg2.extras import execute_values
from app.domain.models import EXPENSE_FOR_INVENTORY
from app.infrastructure.db import ScopedDb

OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_users(
    email TEXT PRIMARY KEY, name TEXT, role TEXT NOT NULL DEFAULT 'VIEWER',
    active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT now(), last_login TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS suppliers(
    id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, contact TEXT);
CREATE TABLE IF NOT EXISTS inventory_items(
    id SERIAL PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL, category TEXT NOT NULL,
    unit TEXT DEFAULT 'unit', qty NUMERIC(12,2) NOT NULL DEFAULT 0 CHECK (qty >= 0),
    reorder_level NUMERIC(12,2) DEFAULT 0, unit_cost NUMERIC(12,2) DEFAULT 0, supplier_id INT REFERENCES suppliers(id));
CREATE TABLE IF NOT EXISTS inventory_moves(
    event_id TEXT PRIMARY KEY, item_id INT REFERENCES inventory_items(id), ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    delta NUMERIC(12,2) NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders(
    id SERIAL PRIMARY KEY, supplier_id INT REFERENCES suppliers(id), item_id INT REFERENCES inventory_items(id),
    qty NUMERIC(12,2) NOT NULL CHECK (qty > 0), unit_cost NUMERIC(12,2) NOT NULL CHECK (unit_cost >= 0),
    status TEXT NOT NULL DEFAULT 'ORDERED', ordered_by TEXT, created_at TIMESTAMPTZ DEFAULT now(), received_at TIMESTAMPTZ);
CREATE INDEX IF NOT EXISTS ix_po_status ON purchase_orders(status, created_at DESC);
CREATE TABLE IF NOT EXISTS employees(
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, department TEXT,
    monthly_salary NUMERIC(12,2) NOT NULL CHECK (monthly_salary >= 0), hired_at DATE, active BOOLEAN NOT NULL DEFAULT TRUE);
CREATE TABLE IF NOT EXISTS employee_performance(
    id SERIAL PRIMARY KEY, employee_id INT REFERENCES employees(id), ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    quality_score REAL CHECK (quality_score BETWEEN 1 AND 5), attendance_pct REAL CHECK (attendance_pct BETWEEN 0 AND 100),
    tasks_completed INT CHECK (tasks_completed >= 0), notes TEXT);
CREATE INDEX IF NOT EXISTS ix_perf_emp_ts ON employee_performance(employee_id, ts DESC);
"""


def _df(cur) -> pd.DataFrame:
    return pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description])


def _num(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = df[c].astype(float)
    return df


class OpsRepository:
    def __init__(self, db: ScopedDb):
        self._conn = db.conn

    def init_schema(self) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute(OPS_SCHEMA)

    # ---- users / roles ----
    def get_user(self, email: str) -> dict | None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT email,name,role,active FROM app_users WHERE email=%s", (email.lower(),))
            r = cur.fetchone()
        return dict(zip(("email", "name", "role", "active"), r)) if r else None

    def upsert_user(self, email: str, name: str, role: str, active: bool = True) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO app_users(email,name,role,active) VALUES(%s,%s,%s,%s) "
                        "ON CONFLICT (email) DO UPDATE SET name=EXCLUDED.name, role=EXCLUDED.role, active=EXCLUDED.active",
                        (email.lower(), name, role, active))

    def touch_login(self, email: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE app_users SET last_login=now() WHERE email=%s", (email.lower(),))

    def list_users(self) -> pd.DataFrame:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT email,name,role,active,last_login FROM app_users ORDER BY email")
            return _df(cur)

    def count_admins(self) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app_users WHERE role='ADMIN' AND active")
            return cur.fetchone()[0]

    # ---- suppliers & inventory ----
    def list_suppliers(self) -> pd.DataFrame:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT id,name,contact FROM suppliers ORDER BY name")
            return _df(cur)

    def add_supplier(self, name: str, contact: str) -> int:
        try:
            with self._conn() as c, c.cursor() as cur:
                cur.execute("INSERT INTO suppliers(name,contact) VALUES(%s,%s) RETURNING id", (name, contact))
                return cur.fetchone()[0]
        except UniqueViolation:
            raise ValueError("Supplier already exists")

    def list_items(self, low_only: bool = False) -> pd.DataFrame:
        q = ("SELECT i.id,i.sku,i.name,i.category,i.unit,i.qty,i.reorder_level,i.unit_cost,i.supplier_id,"
             "s.name AS supplier, (i.qty <= i.reorder_level) AS low FROM inventory_items i "
             "LEFT JOIN suppliers s ON s.id=i.supplier_id")
        if low_only:
            q += " WHERE i.qty <= i.reorder_level"
        with self._conn() as c, c.cursor() as cur:
            cur.execute(q + " ORDER BY i.category, i.name")
            return _num(_df(cur), "qty", "reorder_level", "unit_cost")

    def add_item(self, sku, name, category, unit, qty, reorder_level, unit_cost, supplier_id) -> int:
        try:
            with self._conn() as c, c.cursor() as cur:
                cur.execute("INSERT INTO inventory_items(sku,name,category,unit,qty,reorder_level,unit_cost,supplier_id) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                            (sku, name, category, unit, qty, reorder_level, unit_cost, supplier_id))
                item_id = cur.fetchone()[0]
                if qty:
                    cur.execute("INSERT INTO inventory_moves(event_id,item_id,delta,reason) VALUES(%s,%s,%s,%s)",
                                (uuid.uuid4().hex, item_id, qty, "Opening stock"))
                return item_id
        except UniqueViolation:
            raise ValueError("SKU already exists")

    def adjust_stock(self, item_id: int, delta: float, reason: str) -> float:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE inventory_items SET qty = qty + %s WHERE id=%s AND qty + %s >= 0 RETURNING qty",
                        (delta, item_id, delta))
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT 1 FROM inventory_items WHERE id=%s", (item_id,))
                raise ValueError("Item not found" if not cur.fetchone() else "Insufficient stock for this adjustment")
            cur.execute("INSERT INTO inventory_moves(event_id,item_id,delta,reason) VALUES(%s,%s,%s,%s)",
                        (uuid.uuid4().hex, item_id, delta, reason))
            return float(row[0])

    def inventory_value(self) -> float:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(qty*unit_cost),0) FROM inventory_items")
            return float(cur.fetchone()[0])

    # ---- procurement ----
    def create_po(self, supplier_id: int, item_id: int, qty: float, unit_cost: float, ordered_by: str) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM suppliers WHERE id=%s", (supplier_id,))
            if not cur.fetchone():
                raise ValueError("Supplier not found")
            cur.execute("SELECT 1 FROM inventory_items WHERE id=%s", (item_id,))
            if not cur.fetchone():
                raise ValueError("Item not found")
            cur.execute("INSERT INTO purchase_orders(supplier_id,item_id,qty,unit_cost,ordered_by) "
                        "VALUES(%s,%s,%s,%s,%s) RETURNING id", (supplier_id, item_id, qty, unit_cost, ordered_by))
            return cur.fetchone()[0]

    def list_pos(self, status: str | None = None, limit: int = 200) -> pd.DataFrame:
        q = ("SELECT p.id,s.name AS supplier,i.name AS item,p.qty,p.unit_cost,(p.qty*p.unit_cost) AS total,"
             "p.status,p.ordered_by,p.created_at,p.received_at FROM purchase_orders p "
             "JOIN suppliers s ON s.id=p.supplier_id JOIN inventory_items i ON i.id=p.item_id")
        params: list = []
        if status:
            q += " WHERE p.status=%s"
            params.append(status)
        with self._conn() as c, c.cursor() as cur:
            cur.execute(q + " ORDER BY p.created_at DESC LIMIT %s", params + [limit])
            return _num(_df(cur), "qty", "unit_cost", "total")

    def receive_po(self, po_id: int) -> dict:
        """One transaction: mark received, add stock, post the expense to the ledger (idempotent event ids)."""
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT p.status,p.item_id,p.qty,p.unit_cost,i.category,i.name FROM purchase_orders p "
                        "JOIN inventory_items i ON i.id=p.item_id WHERE p.id=%s FOR UPDATE OF p", (po_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Purchase order not found")
            status, item_id, qty, cost, cat, name = row
            if status != "ORDERED":
                raise ValueError(f"Purchase order is already {status}")
            total = round(float(qty) * float(cost), 2)
            cur.execute("UPDATE purchase_orders SET status='RECEIVED', received_at=now() WHERE id=%s", (po_id,))
            cur.execute("UPDATE inventory_items SET qty=qty+%s, unit_cost=%s WHERE id=%s", (qty, cost, item_id))
            cur.execute("INSERT INTO inventory_moves(event_id,item_id,delta,reason) VALUES(%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING", (f"po-move-{po_id}", item_id, qty, f"PO #{po_id} received"))
            eid = f"po-{po_id}"
            cur.execute("INSERT INTO ledger(event_id,resident_id,ts,kind,category,description,amount,receipt_no) "
                        "VALUES(%s,NULL,now(),'EXPENSE',%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (eid, EXPENSE_FOR_INVENTORY.get(cat, "Other Expense"), f"PO #{po_id}: {name}", total,
                         f"PO-{po_id:06d}"))
            return {"po_id": po_id, "item": name, "qty": float(qty), "total": total}

    def cancel_po(self, po_id: int) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE purchase_orders SET status='CANCELLED' WHERE id=%s AND status='ORDERED'", (po_id,))
            if not cur.rowcount:
                raise ValueError("Only ORDERED purchase orders can be cancelled")

    # ---- HR ----
    def list_employees(self, active_only: bool = True) -> pd.DataFrame:
        q = "SELECT id,name,role,department,monthly_salary,hired_at,active FROM employees"
        if active_only:
            q += " WHERE active"
        with self._conn() as c, c.cursor() as cur:
            cur.execute(q + " ORDER BY id")
            return _num(_df(cur), "monthly_salary")

    def add_employee(self, name, role, department, salary, hired_at) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("INSERT INTO employees(name,role,department,monthly_salary,hired_at) "
                        "VALUES(%s,%s,%s,%s,%s) RETURNING id", (name, role, department, salary, hired_at))
            return cur.fetchone()[0]

    def set_employee_active(self, emp_id: int, active: bool) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("UPDATE employees SET active=%s WHERE id=%s", (active, emp_id))
            if not cur.rowcount:
                raise ValueError("Employee not found")

    def add_performance(self, emp_id: int, quality: float, attendance: float, tasks: int, notes: str) -> None:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM employees WHERE id=%s", (emp_id,))
            if not cur.fetchone():
                raise ValueError("Employee not found")
            cur.execute("INSERT INTO employee_performance(employee_id,quality_score,attendance_pct,tasks_completed,notes) "
                        "VALUES(%s,%s,%s,%s,%s)", (emp_id, quality, attendance, tasks, notes))

    def performance_window(self, days: int) -> pd.DataFrame:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT employee_id,ts,quality_score,attendance_pct,tasks_completed FROM employee_performance "
                        "WHERE ts > now() - make_interval(days => %s)", (days,))
            return _num(_df(cur), "quality_score", "attendance_pct")

    def bulk_seed(self, suppliers, items, employees, perf) -> None:
        with self._conn() as c, c.cursor() as cur:
            execute_values(cur, "INSERT INTO suppliers(name,contact) VALUES %s ON CONFLICT DO NOTHING", suppliers)
            execute_values(cur, "INSERT INTO inventory_items(sku,name,category,unit,qty,reorder_level,unit_cost,supplier_id) "
                                "VALUES %s ON CONFLICT DO NOTHING", items)
            execute_values(cur, "INSERT INTO employees(name,role,department,monthly_salary,hired_at) VALUES %s", employees)
            execute_values(cur, "INSERT INTO employee_performance(employee_id,ts,quality_score,attendance_pct,tasks_completed) "
                                "VALUES %s", perf)

    def count_employees(self) -> int:
        with self._conn() as c, c.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM employees")
            return cur.fetchone()[0]
