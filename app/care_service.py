"""Application service (facade). UI talks only to this class; business rules stay private."""
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pandas as pd
from app.config import Settings
from app.domain.models import (VitalEvent, MedicationEvent, StayEvent, LedgerEvent, PointsEvent,
                               INCOME_CATEGORIES, EXPENSE_CATEGORIES)
from app.infrastructure.cache import RedisCache
from app.infrastructure.outbox import Outbox
from app.infrastructure.postgres import PostgresRepository
from app.services.insights import InsightEngine

log = logging.getLogger("eldercare")
_TABLES = {"vital": "vitals", "med": "med_log", "ledger": "ledger"}
_WELCOME_POINTS, _NIGHT_POINTS, _SPEND_PER_POINT = 50, 10, 100
_TIERS = ((3000, "Platinum"), (1500, "Gold"), (500, "Silver"), (0, "Member"))
_CACHE_PREFIXES = ("dash:", "res:", "fin:")


class CareService:
    def __init__(self, repo: PostgresRepository, cache: RedisCache, outbox: Outbox,
                 engine: InsightEngine, cfg: Settings):
        self.__repo, self.__cache, self.__outbox = repo, cache, outbox
        self.__engine, self.__cfg = engine, cfg

    # ================= public API =================
    def status(self) -> dict:
        return {"postgres": self.__repo.ping(), "redis": self.__cache.alive(),
                "pending_sync": self.__outbox.pending_count()}

    def categories(self) -> dict:
        return {"INCOME": INCOME_CATEGORIES, "EXPENSE": EXPENSE_CATEGORIES}

    def dashboard(self) -> dict:
        cached = self.__cache.get("dash:v2")
        if cached:
            return cached
        risk = self.__engine.daily_risk(self.__repo.vitals_window(14))
        total, in_house = self.__repo.counts()
        month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        fin = self.__repo.finance_summary(month, datetime.now(timezone.utc) + timedelta(days=1))
        has = len(risk) > 0
        payload = {
            "total": total, "in_house": in_house,
            "red": int((risk["level"] == "Red").sum()) if has else 0,
            "amber": int((risk["level"] == "Amber").sum()) if has else 0,
            "income": float(fin.loc[fin.kind == "INCOME", "total"].sum()),
            "expense": float(fin.loc[fin.kind == "EXPENSE", "total"].sum()),
            "top": risk.head(25)[["resident_id", "risk_score", "level", "reasons"]]
                       .astype({"level": str}).to_dict("records") if has else [],
        }
        self.__cache.set("dash:v2", payload)
        return payload

    def residents(self, search: str = "", page: int = 0, size: int = 50) -> pd.DataFrame:
        key = f"res:{search}:{page}:{size}"
        hit = self.__cache.get(key)
        if hit is not None:
            return pd.DataFrame(hit)
        df = self.__repo.list_residents(search, page * size, size)
        self.__cache.set(key, df.to_dict("records"), ttl=300)
        return df

    def resident_detail(self, rid: int) -> dict:
        profile = self.__lookup(rid)
        if profile is None:
            raise ValueError("Cannot load resident while offline")
        hist = self.__repo.resident_history(rid)
        return {"profile": profile, "tier": self.__tier(profile.get("points") or 0),
                "insights": self.__engine.patterns(hist["vitals"]), **hist}

    # ---- admissions ----
    def add_resident(self, name: str, age: int, room: str, conditions: str, diet: str,
                     checkin_at: datetime | None = None) -> int:
        if not name.strip() or not room.strip() or not (50 <= age <= 120):
            raise ValueError("Name and room are required; age must be 50-120")
        rid = self.__repo.add_resident(name.strip(), age, room.strip(), conditions, diet)  # needs DB (new id)
        self.__cache.invalidate(*_CACHE_PREFIXES)
        if checkin_at:
            self.check_in(rid, checkin_at, online=True)
        return rid

    def check_in(self, rid: int, when: datetime, online: bool = True) -> dict:
        r = self.__lookup(rid)
        if r and r["status"] == "IN":
            raise ValueError("Resident is already checked in")
        when = self.__aware(when)
        self.__queue("stay", StayEvent(rid, "IN", when.isoformat()))
        self.__queue("points", PointsEvent(rid, _WELCOME_POINTS, "Welcome bonus", when.isoformat()))
        return {"status": self.__flush(online), "points": _WELCOME_POINTS}

    def check_out(self, rid: int, when: datetime, online: bool = True) -> dict:
        r, when = self.__lookup(rid), self.__aware(when)
        nights = 1
        if r:
            if r["status"] != "IN":
                raise ValueError("Resident is not currently checked in")
            if r["checkin_at"] and when < r["checkin_at"]:
                raise ValueError("Check-out cannot be before check-in")
            if r["checkin_at"]:
                nights = max(1, (when - r["checkin_at"]).days)
        pts = nights * _NIGHT_POINTS
        self.__queue("stay", StayEvent(rid, "OUT", when.isoformat()))
        self.__queue("points", PointsEvent(rid, pts, f"Loyalty: {nights} night(s)", when.isoformat()))
        return {"status": self.__flush(online), "nights": nights, "points": pts}

    # ---- finance ----
    def record_transaction(self, rid: int | None, kind: str, category: str, description: str,
                           amount: float, when: datetime, online: bool = True) -> dict:
        if kind not in ("INCOME", "EXPENSE") or amount <= 0:
            raise ValueError("Kind must be INCOME/EXPENSE and amount must be positive")
        if category not in (INCOME_CATEGORIES + EXPENSE_CATEGORIES):
            raise ValueError("Unknown category")
        when = self.__aware(when)
        ev = LedgerEvent(rid or None, kind, category, description.strip(), round(float(amount), 2),
                         when.isoformat())
        ev = replace(ev, receipt_no=f"RC-{when:%Y%m%d}-{ev.event_id[:6].upper()}")
        self.__queue("ledger", ev)
        pts = int(amount // _SPEND_PER_POINT) if (kind == "INCOME" and rid) else 0
        if pts:
            self.__queue("points", PointsEvent(rid, pts, f"Payment {ev.receipt_no}", when.isoformat()))
        return {**ev.to_dict(), "points": pts, "status": self.__flush(online)}

    def statement(self, rid: int | None, start: datetime, end: datetime) -> dict:
        df = self.__repo.ledger(rid, start, end + timedelta(days=1))
        inc = float(df.loc[df.kind == "INCOME", "amount"].sum())
        exp = float(df.loc[df.kind == "EXPENSE", "amount"].sum())
        return {"table": df, "income": inc, "expense": exp, "net": inc - exp}

    def finance_summary(self, start: datetime, end: datetime) -> pd.DataFrame:
        key = f"fin:{start:%Y%m%d}:{end:%Y%m%d}"
        hit = self.__cache.get(key)
        if hit is not None:
            return pd.DataFrame(hit)
        df = self.__repo.finance_summary(start, end + timedelta(days=1))
        self.__cache.set(key, df.to_dict("records"))
        return df

    def receipt_text(self, e: dict) -> str:
        rid, name = e.get("resident_id"), "Centre (general)"
        if rid is not None and not pd.isna(rid):
            try:
                r = self.__lookup(int(rid))
                name = f"#{int(rid)} {r['name']}" if r else f"#{int(rid)}"
            except ValueError:
                name = f"#{int(rid)}"
        kind = "Payment received" if e["kind"] == "INCOME" else "Payment made"
        w = 38
        return "\n".join([
            self.__cfg.centre_name.center(w), "=" * w,
            f"Receipt No : {e['receipt_no']}",
            f"Date/Time  : {pd.Timestamp(e['ts']):%Y-%m-%d %H:%M}",
            f"Resident   : {name}", f"Type       : {e['kind']} ({kind})",
            f"Category   : {e['category']}", f"Details    : {e.get('description') or '-'}",
            "-" * w, f"AMOUNT     : {self.__cfg.currency} {float(e['amount']):,.2f}", "=" * w,
            "Thank you.".center(w)])

    # ---- points ----
    def award_points(self, rid: int, points: int, reason: str, online: bool = True) -> str:
        if points == 0 or not reason.strip():
            raise ValueError("Points must be non-zero and a reason is required")
        r = self.__lookup(rid)
        if r and points < 0 and (r.get("points") or 0) + points < 0:
            raise ValueError("Insufficient points balance")
        self.__queue("points", PointsEvent(rid, points, reason.strip()))
        return self.__flush(online)

    # ---- care events ----
    def record_vital(self, ev: VitalEvent, online: bool = True) -> str:
        self.__validate_vital(ev)
        self.__queue("vital", ev)
        return self.__flush(online)

    def log_medication(self, ev: MedicationEvent, online: bool = True) -> str:
        if ev.status not in ("GIVEN", "REFUSED", "MISSED"):
            raise ValueError("Invalid medication status")
        self.__queue("med", ev)
        return self.__flush(online)

    def recent_stays(self, limit: int = 100) -> pd.DataFrame:
        return self.__repo.recent_stays(limit)

    def sync(self) -> int:
        """Push offline events to PostgreSQL in ordered batches (idempotent)."""
        total = 0
        while True:
            batch = self.__outbox.pending(self.__cfg.sync_batch)
            if not batch:
                break
            try:
                self.__apply(batch)
                self.__outbox.mark_synced([e for e, _, _ in batch])
                total += len(batch)
            except Exception as exc:       # network down: keep events queued
                log.warning("sync deferred: %s", exc)
                break
        if total:
            self.__cache.invalidate(*_CACHE_PREFIXES)
        return total

    # ================= private business logic =================
    def __queue(self, kind: str, ev) -> None:
        self.__outbox.add(kind, ev.event_id, ev.to_dict())     # always local first

    def __flush(self, online: bool) -> str:
        if online:
            self.sync()
        return "synced" if self.__outbox.pending_count() == 0 else "queued offline"

    def __apply(self, batch: list) -> None:
        for kind, table in _TABLES.items():
            rows = [p for _, k, p in batch if k == kind]
            if rows:
                self.__repo.bulk_insert(table, rows)
        stays = [p for _, k, p in batch if k == "stay"]
        if stays:
            self.__repo.apply_stays(stays)            # order preserved
        pts = [p for _, k, p in batch if k == "points"]
        if pts:
            self.__repo.apply_points(pts)

    def __lookup(self, rid: int) -> dict | None:
        """Resident record, or None when the DB is unreachable (offline). Raises if it truly doesn't exist."""
        try:
            r = self.__repo.get_resident(rid)
        except Exception:
            return None
        if r is None:
            raise ValueError(f"Resident {rid} not found")
        return r

    @staticmethod
    def __aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    @staticmethod
    def __tier(points: int) -> str:
        return next(name for floor, name in _TIERS if points >= floor)

    @staticmethod
    def __validate_vital(ev: VitalEvent) -> None:
        if not (30 <= ev.hr <= 220 and 30.0 <= ev.temp <= 43.0 and 50 <= ev.spo2 <= 100
                and 1 <= ev.mood <= 5 and 0 <= ev.sleep_hrs <= 24):
            raise ValueError("Vital signs out of plausible range")
