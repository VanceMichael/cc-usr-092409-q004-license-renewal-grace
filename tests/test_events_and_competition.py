"""监管事件只追加、案卷竞争唯一生效、迟到回执不倒改。"""

import pytest

from pharmacy_identity.service import DomainError
from tests.conftest import basic_material, seed_world


def _submit(svc, docket_no, **kwargs):
    defaults = dict(
        docket_no=docket_no, store_code="S1", license_number="L1",
        operator_code="OP1", created_by="alice",
        materials_doc=[basic_material()],
    )
    defaults.update(kwargs)
    return svc.submit_batch(**defaults)


def test_only_append_on_terminal_dossier(svc):
    seed_world(svc)
    _submit(svc, "D1")
    svc.record_event(docket_no="D1", kind="rejected",
                     occurred_at="2026-09-05T00:00:00", client_event_id="r1")
    with pytest.raises(DomainError):
        svc.record_event(docket_no="D1", kind="accepted",
                         occurred_at="2026-09-06T00:00:00", client_event_id="a1")


def test_duplicate_regulatory_receipt_is_idempotent(svc):
    seed_world(svc)
    _submit(svc, "D1")
    first = svc.record_event(docket_no="D1", kind="accepted",
                             occurred_at="2026-09-01T00:00:00",
                             client_event_id="rcpt-1")
    second = svc.record_event(docket_no="D1", kind="accepted",
                              occurred_at="2026-09-01T00:00:00",
                              client_event_id="rcpt-1")
    assert first["event_id"] == second["event_id"]
    assert second["deduplicated"] is True


def test_two_competing_dossiers_have_single_effective_one(svc):
    seed_world(svc)
    _submit(svc, "D1")
    _submit(svc, "D2", created_by="mallory")

    # 只有先提交的 D1 是唯一生效案卷；D2 不能签署放行。
    assert svc.channel_status("S1")["docket_no"] == "D1"
    with pytest.raises(DomainError):
        svc.sign_off(docket_no="D2", role="compliance", signer="bob")

    # D1 驳回 → 释放唯一生效位，D2 按提交顺序晋升为生效案卷。
    svc.record_event(docket_no="D1", kind="rejected",
                     occurred_at="2026-09-10T00:00:00", client_event_id="r1")
    assert svc.channel_status("S1")["docket_no"] == "D2"


def test_late_receipt_does_not_rewrite_prior_decisions(svc, fake_clock):
    # 旧证 8 月 31 日截止，续办在缓冲期内；驳回到达后应立即关闭。
    seed_world(svc, expires_at="2026-08-31T00:00:00")
    _submit(svc, "D1")
    # D1 受理并双签，实体店在受理缓冲期开放。
    svc.record_event(docket_no="D1", kind="accepted",
                     occurred_at="2026-09-01T00:00:00", client_event_id="a1")
    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    svc.sign_off(docket_no="D1", role="business", signer="carol")

    # 双签完成后的当时：实体店在受理缓冲期开放。
    open_as_of = fake_clock.peek()
    assert svc.channel_status("S1", open_as_of)["channels"]["physical"]["state"] == "open"

    # 一张声称 8 月 31 日就已驳回的纸质回执，此刻才送达系统。
    fake_clock.tick(seconds=10)
    svc.record_event(docket_no="D1", kind="rejected",
                     occurred_at="2026-08-31T00:00:00", client_event_id="late-r")

    # 历史日期的解释不被倒改：当时仍据受理裁定为开放。
    assert svc.channel_status("S1", open_as_of)["channels"]["physical"]["state"] == "open"
    # 现在（收到驳回后）渠道立即关闭。
    assert svc.channel_status("S1")["channels"]["physical"]["state"] == "closed"


def test_rejected_dossier_releases_and_promotes_competitor(svc):
    seed_world(svc)
    _submit(svc, "D1")
    _submit(svc, "D2")
    svc.record_event(docket_no="D1", kind="rejected",
                     occurred_at="2026-09-10T00:00:00", client_event_id="r1")
    svc.record_event(docket_no="D2", kind="accepted",
                     occurred_at="2026-09-11T00:00:00", client_event_id="a2")
    status = svc.channel_status("S1")
    assert status["docket_no"] == "D2"
    # D2 受理但尚未双签：实体店只能受限，不能凭单签开放。
    assert status["channels"]["physical"]["state"] == "restricted"
