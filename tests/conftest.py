"""测试夹具：内存 SQLite + 可推进的固定时钟。"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from pharmacy_identity import clock, create_app
from pharmacy_identity.schema import metadata
from pharmacy_identity.service import RenewalService


class FakeClock:
    """每次调用 now() 前进一秒，保证不同写操作的 recorded_at 严格有序。"""

    def __init__(self, start: str = "2026-09-02T00:00:00"):
        self.t = datetime.strptime(start, clock.FORMAT)

    def now(self) -> str:
        value = self.t.strftime(clock.FORMAT)
        self.t += timedelta(seconds=1)
        return value

    def tick(self, **delta) -> str:
        self.t += timedelta(**delta)
        return self.t.strftime(clock.FORMAT)

    def jump(self, value: str) -> str:
        self.t = datetime.strptime(value, clock.FORMAT)
        return self.peek()

    def peek(self) -> str:
        return self.t.strftime(clock.FORMAT)


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


@pytest.fixture
def engine():
    db = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(db)
    return db


@pytest.fixture
def svc(engine, fake_clock):
    return RenewalService(engine)


@pytest.fixture
def client(engine, fake_clock):
    return create_app(engine).test_client()


def seed_world(service: RenewalService, *, store_code: str = "S1",
               license_number: str = "L1", operator_code: str = "OP1",
               expires_at: str = "2027-06-30T00:00:00"):
    """登记一个北京地区、主体、门店与许可证的最小世界。"""
    service.register_region("BJ", "北京")
    service.register_rule_version("BJ", "2026-1", "2026-01-01T00:00:00")
    service.register_operator(operator_code, "回春堂医药")
    service.register_store(store_code, f"门店-{store_code}", "BJ", operator_code)
    service.register_license(license_number, store_code, operator_code,
                             "2025-01-01T00:00:00", expires_at)
    return {"store": store_code, "license": license_number, "operator": operator_code}


def basic_material(doc_key: str = "app_form", content_hash: str = "h1"):
    return {
        "doc_key": doc_key,
        "source_summary": f"{doc_key} 原件扫描，门店柜台提交",
        "content_hash": content_hash,
        "valid_from": "2026-08-01T00:00:00",
    }


def submit_and_accept(service, *, docket_no="D1", store="S1", license="L1",
                      operator="OP1", initiator="alice", accepted_at="2026-09-01T00:00:00"):
    """提交 → 受理 → 合规与业务双签，返回受理后渠道应放行的标准案卷。"""
    service.submit_batch(
        docket_no=docket_no, store_code=store, license_number=license,
        operator_code=operator, created_by=initiator,
        materials_doc=[basic_material()],
    )
    service.record_event(docket_no=docket_no, kind="accepted",
                         occurred_at=accepted_at,
                         client_event_id=f"{docket_no}-accept")
    service.sign_off(docket_no=docket_no, role="compliance", signer="bob")
    service.sign_off(docket_no=docket_no, role="business", signer="carol")
    return docket_no
