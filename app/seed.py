"""Generates 1000 residents with 14 days of vitals (vectorised, bulk-loaded)."""
import uuid
import numpy as np
import pandas as pd
from app.domain.models import VitalEvent  # noqa: F401  (documents the schema)

CONDS = ["Hypertension", "Diabetes", "Dementia", "Arthritis", "Heart disease", "None"]
DIETS = ["Regular", "Diabetic", "Soft", "Low-salt", "Vegetarian"]


def seed(repo, n: int = 1000, days: int = 14) -> None:
    rng = np.random.default_rng(42)
    now = pd.Timestamp.now(tz="UTC")
    stay_days, pts = rng.integers(1, 300, n), rng.integers(0, 2500, n)
    checkins = [(now - pd.Timedelta(days=int(d))).to_pydatetime() for d in stay_days]
    repo.insert_residents([
        (f"Resident {i:04d}", int(rng.integers(65, 98)), f"R{100 + i // 4}-{i % 4}",
         str(rng.choice(CONDS)), str(rng.choice(DIETS)), "IN", checkins[i - 1], int(pts[i - 1]))
        for i in range(1, n + 1)])
    repo.bulk_insert("stays", [dict(event_id=uuid.uuid4().hex, resident_id=i, checkin_at=checkins[i - 1],
                                    checkout_at=None) for i in range(1, n + 1)])
    repo.bulk_insert("points_log", [dict(event_id=uuid.uuid4().hex, resident_id=i, ts=checkins[i - 1],
                                         points=int(pts[i - 1]), reason="Opening balance")
                                    for i in range(1, n + 1)])
    _seed_ledger(repo, rng, now, n)
    ids = np.repeat(np.arange(1, n + 1), days)
    day = np.tile(np.arange(days)[::-1], n)
    ts = pd.Timestamp.now(tz="UTC").floor("D") - pd.to_timedelta(day, unit="D") + pd.Timedelta(hours=8)
    N = n * days
    sleep = rng.normal(7, 1, N).clip(3, 10)
    steps = rng.normal(2500, 700, N).clip(200, None)
    mood = np.clip(np.round(2 + sleep / 3 + rng.normal(0, 0.7, N)), 1, 5)   # embedded pattern
    rows = [dict(event_id=uuid.uuid4().hex, resident_id=int(r), ts=t.isoformat(),
                 hr=int(h), temp=round(float(tp), 1), spo2=int(s), sleep_hrs=round(float(sl), 1),
                 steps=int(st), mood=int(m), fluids_ml=int(f))
            for r, t, h, tp, s, sl, st, m, f in zip(
                ids, ts, rng.normal(74, 9, N), rng.normal(36.7, 0.35, N), rng.normal(96, 1.8, N).clip(85, 100),
                sleep, steps, mood, rng.normal(1500, 350, N).clip(300, None))]
    for i in range(0, N, 5000):
        repo.bulk_insert("vitals", rows[i:i + 5000])


def _seed_ledger(repo, rng, now, n):
    rows = []
    def add(rid, kind, cat, desc, amt, ts):
        eid = uuid.uuid4().hex
        rows.append(dict(event_id=eid, resident_id=rid, ts=ts.isoformat(), kind=kind, category=cat,
                         description=desc, amount=round(float(amt), 2),
                         receipt_no=f"RC-{ts:%Y%m%d}-{eid[:6].upper()}"))
    for i in range(1, n + 1):
        add(i, "INCOME", "Room & Care Fee", "Monthly fee", rng.integers(3000, 6000),
            now - pd.Timedelta(days=int(rng.integers(0, 30))))
    for d in range(30):
        for cat in ("Staff Salary", "Medication", "Food & Supplies", "Utilities"):
            add(None, "EXPENSE", cat, "Daily operating cost", rng.integers(800, 4000), now - pd.Timedelta(days=d))
    for i in range(0, len(rows), 5000):
        repo.bulk_insert("ledger", rows[i:i + 5000])
