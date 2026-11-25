"""REST API: public request pool, platform admin, and tenant data access via API keys.

Run:  uvicorn app.api.main:app --host 0.0.0.0 --port 8000
"""
import hmac
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from typing import Callable
import pandas as pd
from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from app.api import schemas as s
from app.container import Runtime, Tenant, build_runtime
from app.domain.errors import AuthError, Conflict, Forbidden, NotFound, RateLimited
from app.domain.models import VitalEvent, MedicationEvent
from app.domain.platform import PLANS
from app.domain.rbac import can
from app.services.platform_service import Principal

log = logging.getLogger("eldercare.api")
app = FastAPI(title="Elder Care Platform API", version="1.0.0",
              description="Organisations apply through the request pool; once approved they receive an API key "
                          "(X-API-Key) scoped to their own isolated data.")


@lru_cache
def runtime() -> Runtime:
    return build_runtime()


# ------------------------------------------------------------------ error mapping
def _err(status: int, msg: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status, headers=headers)


@app.exception_handler(AuthError)
async def _auth(_: Request, e: AuthError):
    return _err(401, str(e), {"WWW-Authenticate": "ApiKey"})


@app.exception_handler(Forbidden)
async def _forbidden(_: Request, e: Forbidden):
    return _err(403, str(e))


@app.exception_handler(NotFound)
async def _not_found(_: Request, e: NotFound):
    return _err(404, str(e))


@app.exception_handler(Conflict)
async def _conflict(_: Request, e: Conflict):
    return _err(409, str(e))


@app.exception_handler(ValueError)
async def _bad(_: Request, e: ValueError):
    msg = str(e)
    return _err(404 if "not found" in msg.lower() else 422, msg)


@app.exception_handler(RateLimited)
async def _limited(_: Request, e: RateLimited):
    return _err(429, str(e), {"Retry-After": str(e.retry_after), "X-RateLimit-Limit": str(e.limit)})


@app.exception_handler(Exception)
async def _boom(_: Request, e: Exception):
    log.exception("unhandled error")
    return _err(500, "Internal error")          # never leak SQL/stack details to callers


# ------------------------------------------------------------------ helpers
def records(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


def _utc(dt: datetime | None) -> datetime:
    dt = dt or datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _day(d: date) -> datetime:
    return datetime.combine(d, dtime.min, tzinfo=timezone.utc)


def client_ip(request: Request) -> str:
    # Behind Render/k8s ingress the real client is the first X-Forwarded-For hop.
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown"))[:64]


# ------------------------------------------------------------------ auth dependencies
def platform_admin(authorization: str | None = Header(default=None)) -> str:
    token = runtime().cfg.admin_token
    if not token:
        raise Forbidden("Platform admin access is not configured (set PLATFORM_ADMIN_TOKEN)")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise AuthError("Invalid admin token")
    return "platform-admin"


class TenantAccess:                       # what an authenticated tenant route receives
    def __init__(self, principal: Principal, tenant: Tenant):
        self.principal, self.t = principal, tenant


def tenant_access(permission: str) -> Callable:
    def dep(response: Response, x_api_key: str | None = Header(default=None)) -> TenantAccess:
        rt = runtime()
        p = rt.platform.authenticate(x_api_key)
        if not can(p.role, permission):
            raise Forbidden(f"Role {p.role} lacks permission {permission}")
        info = rt.platform.consume(p)
        response.headers["X-RateLimit-Limit"] = str(info.limit)
        response.headers["X-RateLimit-Remaining"] = str(info.remaining)
        response.headers["X-RateLimit-Reset"] = str(info.reset_seconds)
        return TenantAccess(p, rt.tenants.get(p.schema))
    return dep


# ------------------------------------------------------------------ public
@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/v1/plans")
def plans():
    return {k: {"rate_per_minute": v.rate_per_min, "monthly_quota": v.monthly_quota} for k, v in PLANS.items()}


@app.post("/v1/subscription-requests", status_code=201)
def submit_request(body: s.SubscriptionRequestIn, request: Request):
    return runtime().platform.submit_request(body.org_name, body.contact_name, body.contact_email, body.country,
                                             body.expected_residents, body.use_case, body.plan, client_ip(request))


@app.get("/v1/subscription-requests/{req_id}")
def request_status(req_id: int, x_request_token: str = Header()):
    return runtime().platform.request_status(req_id, x_request_token)


@app.post("/v1/subscription-requests/{req_id}/claim-key")
def claim_key(req_id: int, x_request_token: str = Header()):
    return runtime().platform.claim_key(req_id, x_request_token)


# ------------------------------------------------------------------ platform admin
admin = Depends(platform_admin)


@app.get("/v1/admin/requests")
def admin_requests(status: str | None = Query(default="PENDING"), limit: int = 50, offset: int = 0,
                   _: str = admin):
    return runtime().platform.list_requests(status or None, limit, offset)


@app.post("/v1/admin/requests/{req_id}/approve")
def admin_approve(req_id: int, body: s.ApproveIn, who: str = admin):
    return runtime().platform.approve(req_id, who, body.plan, body.note)


@app.post("/v1/admin/requests/{req_id}/reject")
def admin_reject(req_id: int, body: s.RejectIn, who: str = admin):
    runtime().platform.reject(req_id, who, body.note)
    return {"status": "REJECTED"}


@app.get("/v1/admin/orgs")
def admin_orgs(_: str = admin):
    return runtime().platform.list_orgs()


@app.post("/v1/admin/orgs/{org_id}/provision")
def admin_provision(org_id: int, _: str = admin):
    return runtime().platform.provision_org(org_id)


@app.post("/v1/admin/orgs/{org_id}/suspend")
def admin_suspend(org_id: int, _: str = admin):
    runtime().platform.set_org_active(org_id, False)
    return {"status": "SUSPENDED"}


@app.post("/v1/admin/orgs/{org_id}/reactivate")
def admin_reactivate(org_id: int, _: str = admin):
    runtime().platform.set_org_active(org_id, True)
    return {"status": "ACTIVE"}


@app.get("/v1/admin/orgs/{org_id}/keys")
def admin_keys(org_id: int, _: str = admin):
    return runtime().platform.list_keys(org_id)


@app.post("/v1/admin/orgs/{org_id}/keys", status_code=201)
def admin_issue_key(org_id: int, body: s.KeyIn, _: str = admin):
    return runtime().platform.issue_key(org_id, body.label, body.role)


@app.delete("/v1/admin/keys/{key_id}")
def admin_revoke_key(key_id: int, _: str = admin):
    runtime().platform.revoke_key(key_id)
    return {"status": "REVOKED"}


# ------------------------------------------------------------------ tenant API (X-API-Key)
@app.get("/v1/me")
def me(a: TenantAccess = Depends(tenant_access("dashboard.read"))):
    p = a.principal
    plan = PLANS[p.plan]
    return {"organisation": p.org_name, "plan": p.plan, "role": p.role,
            "rate_per_minute": plan.rate_per_min, "monthly_quota": plan.monthly_quota}


@app.get("/v1/dashboard")
def dashboard(a: TenantAccess = Depends(tenant_access("dashboard.read"))):
    return a.t.care.dashboard()


@app.get("/v1/residents")
def list_residents(search: str = "", page: int = Query(0, ge=0), size: int = Query(50, ge=1, le=200),
                   a: TenantAccess = Depends(tenant_access("residents.read"))):
    return records(a.t.care.residents(search, page, size))


@app.get("/v1/residents/{rid}")
def resident(rid: int, a: TenantAccess = Depends(tenant_access("residents.read"))):
    d = a.t.care.resident_detail(rid)
    frames = {k: records(d[k]) for k in ("vitals", "meds", "stays", "points")}
    return {"profile": d["profile"], "tier": d["tier"], "insights": d["insights"], **frames}


@app.post("/v1/residents", status_code=201)
def add_resident(b: s.ResidentIn, a: TenantAccess = Depends(tenant_access("residents.write"))):
    when = _utc(b.check_in_at) if b.check_in_at else None
    return {"resident_id": a.t.care.add_resident(b.name, b.age, b.room, b.conditions, b.diet, when)}


@app.post("/v1/residents/{rid}/check-in")
def check_in(rid: int, b: s.StayIn, a: TenantAccess = Depends(tenant_access("residents.write"))):
    return a.t.care.check_in(rid, _utc(b.at))


@app.post("/v1/residents/{rid}/check-out")
def check_out(rid: int, b: s.StayIn, a: TenantAccess = Depends(tenant_access("residents.write"))):
    return a.t.care.check_out(rid, _utc(b.at))


@app.post("/v1/residents/{rid}/vitals", status_code=201)
def add_vitals(rid: int, b: s.VitalIn, a: TenantAccess = Depends(tenant_access("care.write"))):
    ev = VitalEvent(rid, b.hr, b.temp, b.spo2, b.sleep_hrs, b.steps, b.mood, b.fluids_ml)
    return {"status": a.t.care.record_vital(ev), "event_id": ev.event_id}


@app.post("/v1/residents/{rid}/medications", status_code=201)
def add_medication(rid: int, b: s.MedicationIn, a: TenantAccess = Depends(tenant_access("care.write"))):
    ev = MedicationEvent(rid, b.drug, b.status)
    return {"status": a.t.care.log_medication(ev), "event_id": ev.event_id}


@app.post("/v1/finance/transactions", status_code=201)
def add_transaction(b: s.TransactionIn, a: TenantAccess = Depends(tenant_access("finance.write"))):
    return a.t.care.record_transaction(b.resident_id, b.kind, b.category, b.description, b.amount, _utc(b.at))


@app.get("/v1/finance/statement")
def statement(start: date | None = None, end: date | None = None, resident_id: int | None = None,
              a: TenantAccess = Depends(tenant_access("finance.read"))):
    end = end or date.today()
    start = start or end - timedelta(days=30)
    r = a.t.care.statement(resident_id, _day(start), _day(end))
    return {"income": r["income"], "expense": r["expense"], "net": r["net"], "entries": records(r["table"])}


@app.get("/v1/finance/summary")
def finance_summary(start: date | None = None, end: date | None = None,
                    a: TenantAccess = Depends(tenant_access("finance.read"))):
    end = end or date.today()
    start = start or end.replace(day=1)
    return records(a.t.care.finance_summary(_day(start), _day(end)))


@app.get("/v1/inventory")
def inventory(low_only: bool = False, a: TenantAccess = Depends(tenant_access("inventory.read"))):
    return records(a.t.ops.list_items(low_only))


@app.get("/v1/procurement/orders")
def purchase_orders(status: str | None = None, a: TenantAccess = Depends(tenant_access("procurement.read"))):
    return records(a.t.ops.list_pos(status))


@app.get("/v1/hr/employees")
def employees(a: TenantAccess = Depends(tenant_access("hr.read"))):
    return records(a.t.ops.list_employees())
