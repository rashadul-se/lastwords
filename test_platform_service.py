"""Unit tests for the request pool, key lifecycle and rate limiting (no Postgres/Redis needed)."""
import sys, os, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.domain.errors import AuthError, Conflict, Forbidden, NotFound, RateLimited
from app.domain.platform import slugify
from app.services.platform_service import PlatformService


class FakeRepo:
    def __init__(self):
        self.reqs, self.orgs, self.keys = {}, {}, {}

    def create_request(self, org_name, contact_name, email, country, expected, use_case, plan, token_hash):
        if any(r["contact_email"] == email and r["status"] == "PENDING" for r in self.reqs.values()):
            raise Conflict("A request for this email is already pending review")
        i = len(self.reqs) + 1
        self.reqs[i] = dict(id=i, org_name=org_name, contact_email=email, plan_requested=plan, status="PENDING",
                            token_hash=token_hash, created_at=datetime.now(timezone.utc), reviewed_at=None,
                            review_note=None, org_id=None, key_claimed=False)
        return i

    def get_request(self, i): return self.reqs.get(i)

    def list_requests(self, status, limit, offset):
        return [r for r in self.reqs.values() if not status or r["status"] == status]

    def approve_request(self, i, plan, reviewer, note):
        r = self.reqs.get(i)
        if not r: raise NotFound("Request not found")
        if r["status"] != "PENDING": raise Conflict(f"Request is already {r['status']}")
        oid = len(self.orgs) + 1
        self.orgs[oid] = dict(id=oid, name=r["org_name"], schema_name=f"org_{oid}_{slugify(r['org_name'])}",
                              plan=plan, status="PROVISIONING")
        r.update(status="APPROVED", org_id=oid, review_note=note)
        return self.orgs[oid]

    def reject_request(self, i, reviewer, note):
        r = self.reqs.get(i)
        if not r: raise NotFound("Request not found")
        if r["status"] != "PENDING": raise Conflict(f"Request is already {r['status']}")
        r["status"] = "REJECTED"

    def claim_key_slot(self, i):
        r = self.reqs[i]
        if r["status"] == "APPROVED" and not r["key_claimed"]:
            r["key_claimed"] = True
            return True
        return False

    def release_key_slot(self, i): self.reqs[i]["key_claimed"] = False
    def get_org(self, oid): return self.orgs.get(oid)
    def list_orgs(self): return list(self.orgs.values())
    def set_org_status(self, oid, s): self.orgs[oid]["status"] = s

    def insert_key(self, org_id, prefix, h, label, role):
        kid = len(self.keys) + 1
        self.keys[prefix] = dict(key_id=kid, key_hash=h, role=role, revoked_at=None, org_id=org_id)
        return kid

    def find_key(self, prefix):
        k = self.keys.get(prefix)
        if not k: return None
        o = self.orgs[k["org_id"]]
        return {**k, "org_name": o["name"], "schema_name": o["schema_name"], "plan": o["plan"],
                "org_status": o["status"]}

    def touch_key(self, kid): pass
    def list_keys(self, oid): return [k for k in self.keys.values() if k["org_id"] == oid]

    def revoke_key(self, kid):
        for k in self.keys.values():
            if k["key_id"] == kid and not k["revoked_at"]:
                k["revoked_at"] = datetime.now(timezone.utc); return
        raise NotFound("Key not found or already revoked")


class FakeCache:
    def __init__(self, up=True): self.n, self.up = {}, up

    def incr_window(self, key, ttl):
        if not self.up: return None
        self.n[key] = self.n.get(key, 0) + 1
        return self.n[key]


APPLY = dict(org_name="Sunrise Homes", contact_name="Ada Obi", email="ada@sunrise.org")


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.repo, self.cache, self.provisioned, self.t = FakeRepo(), FakeCache(), [], [1_700_000_000.0]
        self.svc = PlatformService(self.repo, self.cache, self.provisioned.append, clock=lambda: self.t[0])

    def approved_key(self, **kw):
        sub = self.svc.submit_request(**APPLY, **kw)
        self.svc.approve(sub["request_id"], "admin")
        return sub, self.svc.claim_key(sub["request_id"], sub["status_token"])

    # ---- request pool ----
    def test_submit_validates_and_stores_only_a_token_hash(self):
        sub = self.svc.submit_request(**APPLY)
        self.assertEqual(sub["status"], "PENDING")
        self.assertNotIn(sub["status_token"], str(self.repo.reqs))
        for bad in (dict(APPLY, email="nope"), dict(APPLY, org_name="x"), dict(APPLY, plan="GOLD")):
            with self.assertRaises(ValueError):
                self.svc.submit_request(**bad)

    def test_duplicate_pending_email_conflicts(self):
        self.svc.submit_request(**APPLY)
        with self.assertRaises(Conflict):
            self.svc.submit_request(**APPLY)

    def test_status_needs_the_right_token(self):
        sub = self.svc.submit_request(**APPLY)
        self.assertEqual(self.svc.request_status(sub["request_id"], sub["status_token"])["status"], "PENDING")
        with self.assertRaises(NotFound):
            self.svc.request_status(sub["request_id"], "wrong")
        with self.assertRaises(NotFound):
            self.svc.request_status(999, sub["status_token"])

    def test_public_submission_is_rate_limited_per_ip(self):
        for i in range(5):
            self.svc.submit_request(**dict(APPLY, email=f"a{i}@x.org"), client_ip="1.2.3.4")
        with self.assertRaises(RateLimited):
            self.svc.submit_request(**dict(APPLY, email="a9@x.org"), client_ip="1.2.3.4")
        self.svc.submit_request(**dict(APPLY, email="b@x.org"), client_ip="5.6.7.8")   # other IP unaffected

    # ---- approval / keys ----
    def test_approve_provisions_schema_and_activates(self):
        sub = self.svc.submit_request(**APPLY)
        org = self.svc.approve(sub["request_id"], "admin", plan="STANDARD")
        self.assertEqual((org["status"], org["plan"]), ("ACTIVE", "STANDARD"))
        self.assertEqual(self.provisioned, [org["schema_name"]])
        with self.assertRaises(Conflict):
            self.svc.approve(sub["request_id"], "admin")

    def test_key_cannot_be_claimed_before_approval_or_twice(self):
        sub = self.svc.submit_request(**APPLY)
        with self.assertRaises(Conflict):
            self.svc.claim_key(sub["request_id"], sub["status_token"])
        self.svc.approve(sub["request_id"], "admin")
        self.svc.claim_key(sub["request_id"], sub["status_token"])
        with self.assertRaises(Conflict):
            self.svc.claim_key(sub["request_id"], sub["status_token"])

    def test_failed_issue_releases_the_claim(self):
        sub = self.svc.submit_request(**APPLY)
        self.svc.approve(sub["request_id"], "admin")
        orig, self.repo.insert_key = self.repo.insert_key, lambda *a: 1 / 0
        with self.assertRaises(ZeroDivisionError):
            self.svc.claim_key(sub["request_id"], sub["status_token"])
        self.repo.insert_key = orig
        self.assertIn("api_key", self.svc.claim_key(sub["request_id"], sub["status_token"]))

    def test_reject_blocks_key_claim(self):
        sub = self.svc.submit_request(**APPLY)
        self.svc.reject(sub["request_id"], "admin", "no")
        with self.assertRaises(Conflict):
            self.svc.claim_key(sub["request_id"], sub["status_token"])

    # ---- authentication ----
    def test_authenticate_accepts_valid_key_rejects_others(self):
        _, k = self.approved_key()
        p = self.svc.authenticate(k["api_key"])
        self.assertEqual((p.role, p.plan), ("ADMIN", "FREE"))
        for bad in (None, "", "garbage", "ecp_deadbeef_x", k["api_key"][:-1] + "0", "xyz_" + k["api_key"][4:]):
            with self.assertRaises(AuthError):
                self.svc.authenticate(bad)

    def test_revoked_and_suspended(self):
        sub, k = self.approved_key()
        self.svc.revoke_key(k["key_id"])
        with self.assertRaises(AuthError):
            self.svc.authenticate(k["api_key"])
        k2 = self.svc.issue_key(1, "second", "VIEWER")
        self.svc.set_org_active(1, False)
        with self.assertRaises(Forbidden):
            self.svc.authenticate(k2["api_key"])
        self.svc.set_org_active(1, True)
        self.assertEqual(self.svc.authenticate(k2["api_key"]).role, "VIEWER")

    def test_issue_key_rejects_unknown_role(self):
        self.approved_key()
        with self.assertRaises(ValueError):
            self.svc.issue_key(1, "x", "SUPERUSER")

    # ---- rate limiting ----
    def test_per_minute_limit_and_reset(self):
        _, k = self.approved_key()
        p = self.svc.authenticate(k["api_key"])
        for _ in range(60):
            self.svc.consume(p)
        with self.assertRaises(RateLimited) as cm:
            self.svc.consume(p)
        self.assertGreaterEqual(cm.exception.retry_after, 1)
        self.t[0] += 60                                   # next window
        self.assertEqual(self.svc.consume(p).limit, 60)

    def test_limits_are_per_organisation(self):
        _, k = self.approved_key()
        p1 = self.svc.authenticate(k["api_key"])
        for _ in range(60):
            self.svc.consume(p1)
        other = type(p1)(2, "Other Org", "org_2_other_org", "FREE", "ADMIN", 99)
        self.assertEqual(self.svc.consume(other).remaining, 59)

    def test_falls_back_to_memory_counters_when_redis_is_down(self):
        self.cache.up = False
        _, k = self.approved_key()
        p = self.svc.authenticate(k["api_key"])
        for _ in range(60):
            self.svc.consume(p)
        with self.assertRaises(RateLimited):
            self.svc.consume(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
