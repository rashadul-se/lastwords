"""Composition root: the only place concrete classes are wired together."""
import os
import threading
from dataclasses import dataclass
from app.config import Settings
from app.infrastructure.cache import RedisCache
from app.infrastructure.db import Database
from app.infrastructure.ops_repo import OpsRepository
from app.infrastructure.outbox import Outbox
from app.infrastructure.platform_repo import PlatformRepository
from app.infrastructure.postgres import PostgresRepository
from app.services.care_service import CareService
from app.services.insights import InsightEngine
from app.services.platform_service import PlatformService
from app.seed import seed


@dataclass(frozen=True)
class Tenant:
    schema: str
    care: CareService
    ops: OpsRepository


class TenantRegistry:
    """Builds each organisation's services once per process; every one is pinned to its own schema."""

    def __init__(self, db: Database, cache: RedisCache, cfg: Settings):
        self._db, self._cache, self._cfg = db, cache, cfg
        self._tenants: dict[str, Tenant] = {}
        self._lock = threading.Lock()

    def get(self, schema: str, *, seed_demo: bool = False) -> Tenant:
        t = self._tenants.get(schema)
        if t:
            return t
        with self._lock:
            if schema not in self._tenants:
                self._tenants[schema] = self.__build(schema, seed_demo)
            return self._tenants[schema]

    def provision(self, schema: str) -> None:
        self._db.create_schema(schema)      # validates the name, CREATE SCHEMA IF NOT EXISTS
        self.get(schema)                    # creates the tables

    def __build(self, schema: str, seed_demo: bool) -> Tenant:
        scoped = self._db.scoped(schema)
        repo, ops = PostgresRepository(scoped), OpsRepository(scoped)
        repo.init_schema()
        ops.init_schema()
        if seed_demo and repo.count_residents() == 0:
            seed(repo, self._cfg.resident_count)
        os.makedirs(self._cfg.outbox_dir, exist_ok=True)
        care = CareService(repo, self._cache.scoped(f"t:{schema}"),
                           Outbox(os.path.join(self._cfg.outbox_dir, f"eldercare_{schema}.db")),
                           InsightEngine(), self._cfg)
        return Tenant(schema, care, ops)


@dataclass(frozen=True)
class Runtime:
    cfg: Settings
    platform: PlatformService
    tenants: TenantRegistry


def build_runtime() -> Runtime:
    cfg = Settings()
    db = Database(cfg.database_url, cfg.pool_min, cfg.pool_max)
    db.create_schema(cfg.platform_schema)
    cache = RedisCache(cfg.redis_url, cfg.cache_ttl)
    repo = PlatformRepository(db.scoped(cfg.platform_schema))
    repo.init_schema()
    tenants = TenantRegistry(db, cache, cfg)
    return Runtime(cfg, PlatformService(repo, cache.scoped("platform"), tenants.provision), tenants)


def build_service() -> CareService:
    """Single-tenant service for the Streamlit UI (uses CARE_DEFAULT_SCHEMA, auto-seeds demo data)."""
    cfg = Settings()
    db = Database(cfg.database_url, cfg.pool_min, cfg.pool_max)
    registry = TenantRegistry(db, RedisCache(cfg.redis_url, cfg.cache_ttl), cfg)
    return registry.get(cfg.default_schema, seed_demo=cfg.auto_seed).care
