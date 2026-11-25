"""Platform (multi-organisation) domain: plans, statuses, validation. No I/O."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    rate_per_min: int
    monthly_quota: int


PLANS: dict[str, Plan] = {
    "FREE": Plan(rate_per_min=60, monthly_quota=10_000),
    "STANDARD": Plan(rate_per_min=300, monthly_quota=500_000),
    "ENTERPRISE": Plan(rate_per_min=1200, monthly_quota=10_000_000),
}

REQUEST_STATUSES = ("PENDING", "APPROVED", "REJECTED")
ORG_STATUSES = ("PROVISIONING", "ACTIVE", "SUSPENDED")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or "")) and len(email) <= 254


def slugify(name: str) -> str:
    """Lower-case [a-z0-9_] slug used inside the tenant schema name."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "org"
