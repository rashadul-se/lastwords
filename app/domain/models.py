"""Domain layer: plain immutable data, no I/O."""
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class VitalEvent:
    resident_id: int
    hr: int
    temp: float
    spo2: int
    sleep_hrs: float
    steps: int
    mood: int          # 1 (low) .. 5 (great)
    fluids_ml: int
    ts: str = field(default_factory=_now)
    event_id: str = field(default_factory=_id)   # idempotency key for sync

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MedicationEvent:
    resident_id: int
    drug: str
    status: str        # GIVEN | REFUSED | MISSED
    ts: str = field(default_factory=_now)
    event_id: str = field(default_factory=_id)

    def to_dict(self) -> dict:
        return asdict(self)


INCOME_CATEGORIES = ["Room & Care Fee", "Deposit", "Meal Plan", "Therapy Package", "Donation", "Other Income"]
EXPENSE_CATEGORIES = ["Staff Salary", "Medication", "Food & Supplies", "Utilities", "Maintenance",
                      "Refund", "Transport", "Other Expense"]


@dataclass(frozen=True)
class StayEvent:
    resident_id: int
    action: str            # IN | OUT
    ts: str                # ISO-8601 with timezone
    event_id: str = field(default_factory=_id)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LedgerEvent:
    resident_id: int | None    # None = centre-wide entry
    kind: str                  # INCOME | EXPENSE
    category: str
    description: str
    amount: float
    ts: str = field(default_factory=_now)
    receipt_no: str = ""
    event_id: str = field(default_factory=_id)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PointsEvent:
    resident_id: int
    points: int            # negative = redemption
    reason: str
    ts: str = field(default_factory=_now)
    event_id: str = field(default_factory=_id)

    def to_dict(self) -> dict:
        return asdict(self)


# ---- operations domain ----
INVENTORY_CATEGORIES = ["Food", "Medication", "Cleaning", "Medical supplies", "Maintenance", "Linen"]
# inventory category -> ledger expense category used when a purchase order is received
EXPENSE_FOR_INVENTORY = {"Food": "Food & Supplies", "Medication": "Medication", "Cleaning": "Food & Supplies",
                         "Medical supplies": "Medication", "Maintenance": "Maintenance", "Linen": "Food & Supplies"}
EMPLOYEE_ROLES = ["Nurse", "Caregiver", "Physiotherapist", "Cook", "Cleaner", "Admin staff"]
PO_STATUSES = ("ORDERED", "RECEIVED", "CANCELLED")
