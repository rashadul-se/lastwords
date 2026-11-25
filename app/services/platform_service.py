"""Platform service: subscription request pool, organisation lifecycle, API keys, auth and rate limits.
Depends only on a repository and a cache (duck-typed), so it is unit-testable without Postgres/Redis."""
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable
from app.domain.errors import AuthError, Conflict, Forbidden, NotFound, RateLimited
from app.domain.platform import PLANS, valid_email
from app.domain.rbac import valid_role

KEY_PREFIX = "ecp"
_PUBLIC_REQUESTS_PER_HOUR = 5


@dataclass(frozen=True)
class Principal:
    org_id: int
    org_name: str
    schema: str
    plan: str
    role: str
    key_id: int


@dataclass(frozen=True)
class RateInfo:
    limit: int
    remaining: int
    reset_seconds: int


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PlatformService:
    def __init__(self, repo, cache, provision: Callable[[str], None], clock: Callable[[], float] = time.time):
        self._repo, self._cache, self._provision, self._now = repo, cache, provision, clock
        self._mem: dict[str, int] = {}          # fallback counters when Redis is unavailable
        self._mem_lock = threading.Lock()

    # ================= request pool (public) =================
    def submit_request(self, org_name: str, contact_name: str, email: str, country: str = "",
                       expected_residents: int | None = None, use_case: str = "",
                       plan: str = "FREE", client_ip: str = "unknown") -> dict:
        org_name, contact_name, email = org_name.strip(), contact_name.strip(), email.strip().lower()
        if not (2 <= len(org_name) <= 120) or not (2 <= len(contact_name) <= 120):
            raise ValueError("Organisation and contact names must be 2-120 characters")
        if not valid_email(email):
            raise ValueError("A valid contact email is required")
        if plan not in PLANS:
            raise ValueError(f"Unknown plan; choose one of {', '.join(PLANS)}")
        if expected_residents is not None and not (1 <= expected_residents <= 100_000):
            raise ValueError("Expected residents must be between 1 and 100000")
        if len(use_case) > 1000 or len(country) > 60:
            raise ValueError("Use case max 1000 characters, country max 60")
        self.__limit_public(client_ip)
        token = secrets.token_urlsafe(24)
        req_id = self._repo.create_request(org_name, contact_name, email, country.strip(), expected_residents,
                                           use_case.strip(), plan, _hash(token))
        # The token is the applicant's only credential for checking status: stored hashed, shown once.
        return {"request_id": req_id, "status": "PENDING", "status_token": token}

    def request_status(self, req_id: int, token: str) -> dict:
        r = self.__authorised_request(req_id, token)
        out = {"request_id": r["id"], "status": r["status"], "plan_requested": r["plan_requested"],
               "submitted_at": r["created_at"], "reviewed_at": r["reviewed_at"], "review_note": r["review_note"]}
        if r["status"] == "APPROVED":
            out["key_claimed"] = r["key_claimed"]
        return out

    def claim_key(self, req_id: int, token: str) -> dict:
        """Applicant collects their first API key exactly once; no plaintext key is ever stored."""
        r = self.__authorised_request(req_id, token)
        if r["status"] != "APPROVED":
            raise Conflict(f"Request is {r['status']}; a key can only be claimed after approval")
        if not self._repo.claim_key_slot(req_id):
            raise Conflict("Key already claimed. Ask the platform admin to issue a new key")
        try:
            return self.issue_key(r["org_id"], "initial", "ADMIN")
        except Exception:
            self._repo.release_key_slot(req_id)
            raise

    # ================= admin =================
    def list_requests(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._repo.list_requests(status, max(1, min(limit, 200)), max(0, offset))
        return [{k: v for k, v in r.items() if k != "token_hash"} for r in rows]

    def approve(self, req_id: int, reviewer: str, plan: str | None = None, note: str = "") -> dict:
        req = self._repo.get_request(req_id)
        if not req:
            raise NotFound("Request not found")
        plan = plan or req["plan_requested"]
        if plan not in PLANS:
            raise ValueError(f"Unknown plan; choose one of {', '.join(PLANS)}")
        org = self._repo.approve_request(req_id, plan, reviewer, note)
        return self.provision_org(org["id"])

    def provision_org(self, org_id: int) -> dict:
        """Idempotent: creates the tenant schema + tables, then activates the organisation."""
        org = self._repo.get_org(org_id)
        if not org:
            raise NotFound("Organisation not found")
        self._provision(org["schema_name"])
        if org["status"] == "PROVISIONING":
            self._repo.set_org_status(org_id, "ACTIVE")
        return self._repo.get_org(org_id)

    def reject(self, req_id: int, reviewer: str, note: str = "") -> None:
        self._repo.reject_request(req_id, reviewer, note)

    def list_orgs(self) -> list[dict]:
        return self._repo.list_orgs()

    def set_org_active(self, org_id: int, active: bool) -> None:
        org = self._repo.get_org(org_id)
        if not org:
            raise NotFound("Organisation not found")
        if org["status"] == "PROVISIONING":
            raise Conflict("Organisation is still provisioning; run provision first")
        self._repo.set_org_status(org_id, "ACTIVE" if active else "SUSPENDED")

    def issue_key(self, org_id: int, label: str, role: str = "ADMIN") -> dict:
        if not valid_role(role):
            raise ValueError("Unknown role")
        if not self._repo.get_org(org_id):
            raise NotFound("Organisation not found")
        prefix, secret = secrets.token_hex(4), secrets.token_hex(24)
        key_id = self._repo.insert_key(org_id, prefix, _hash(secret), label.strip()[:80], role)
        return {"key_id": key_id, "api_key": f"{KEY_PREFIX}_{prefix}_{secret}", "role": role,
                "note": "Store this key now; it cannot be shown again."}

    def list_keys(self, org_id: int) -> list[dict]:
        return self._repo.list_keys(org_id)

    def revoke_key(self, key_id: int) -> None:
        self._repo.revoke_key(key_id)

    # ================= tenant authentication + limits =================
    def authenticate(self, raw_key: str | None) -> Principal:
        parts = (raw_key or "").split("_")
        if len(parts) != 3 or parts[0] != KEY_PREFIX or len(parts[1]) != 8:
            raise AuthError("Missing or malformed API key")
        row = self._repo.find_key(parts[1])
        # Same error for unknown prefix / wrong secret / revoked, so keys cannot be probed.
        if not row or row["revoked_at"] or not hmac.compare_digest(row["key_hash"], _hash(parts[2])):
            raise AuthError("Invalid API key")
        if row["org_status"] != "ACTIVE":
            raise Forbidden("Organisation is not active")
        try:
            self._repo.touch_key(row["key_id"])
        except Exception:
            pass                                   # bookkeeping must never fail a request
        return Principal(row["org_id"], row["org_name"], row["schema_name"], row["plan"], row["role"],
                         row["key_id"])

    def consume(self, p: Principal) -> RateInfo:
        """Per-minute rate limit + monthly quota for the organisation (shared across all its keys)."""
        plan, now = PLANS[p.plan], int(self._now())
        window = now // 60
        used = self.__incr(f"rl:{p.org_id}:{window}", 90)
        reset = 60 - (now % 60)
        if used > plan.rate_per_min:
            raise RateLimited("Rate limit exceeded", retry_after=reset, limit=plan.rate_per_min)
        month = time.strftime("%Y%m", time.gmtime(now))
        if self.__incr(f"quota:{p.org_id}:{month}", 40 * 86400) > plan.monthly_quota:
            raise RateLimited("Monthly quota exceeded", retry_after=86400, limit=plan.monthly_quota)
        return RateInfo(plan.rate_per_min, max(0, plan.rate_per_min - used), reset)

    # ================= internals =================
    def __authorised_request(self, req_id: int, token: str) -> dict:
        r = self._repo.get_request(req_id)
        # Wrong id and wrong token are indistinguishable to the caller.
        if not r or not hmac.compare_digest(r["token_hash"], _hash(token or "")):
            raise NotFound("Request not found")
        return r

    def __limit_public(self, ip: str) -> None:
        hour = int(self._now()) // 3600
        if self.__incr(f"pub:{ip}:{hour}", 3700) > _PUBLIC_REQUESTS_PER_HOUR:
            raise RateLimited("Too many requests from this address", retry_after=3600,
                              limit=_PUBLIC_REQUESTS_PER_HOUR)

    def __incr(self, key: str, ttl: int) -> int:
        n = self._cache.incr_window(key, ttl)
        if n is not None:
            return n
        with self._mem_lock:                         # Redis down: per-process counters (approximate)
            if len(self._mem) > 10_000:
                self._mem.clear()
            self._mem[key] = self._mem.get(key, 0) + 1
            return self._mem[key]
