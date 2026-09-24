"""双签缓冲放行规则。"""

import pytest

from pharmacy_identity.service import DomainError
from tests.conftest import basic_material, seed_world


def _submit_and_accept(svc, docket_no="D1", initiator="alice"):
    svc.submit_batch(
        docket_no=docket_no, store_code="S1", license_number="L1",
        operator_code="OP1", created_by=initiator,
        materials_doc=[basic_material()],
    )
    svc.record_event(docket_no=docket_no, kind="accepted",
                     occurred_at="2026-09-01T00:00:00", client_event_id="a1")


def test_initiator_cannot_self_approve(svc):
    seed_world(svc)
    _submit_and_accept(svc)
    with pytest.raises(DomainError):
        svc.sign_off(docket_no="D1", role="compliance", signer="alice")


def test_two_different_signers_required(svc):
    seed_world(svc)
    _submit_and_accept(svc)
    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    # 同一人不能占两个签署角色。
    with pytest.raises(DomainError):
        svc.sign_off(docket_no="D1", role="business", signer="bob")


def test_single_signoff_keeps_physical_restricted(svc):
    seed_world(svc)
    _submit_and_accept(svc)
    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    channels = svc.channel_status("S1")["channels"]
    assert channels["physical"]["state"] == "restricted"
    assert channels["physical"]["reason"] == "awaiting_signoff:business"


def test_dual_signoff_opens_temporary_channels(svc):
    seed_world(svc)
    _submit_and_accept(svc)
    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    svc.sign_off(docket_no="D1", role="business", signer="carol")
    channels = svc.channel_status("S1")["channels"]
    assert channels["physical"]["state"] == "open"
    # 默认规则：受理缓冲期线上店仅可受限运营、配送亦然。
    assert channels["online"]["state"] == "restricted"
    assert channels["delivery"]["state"] == "restricted"


def test_signoff_bound_to_revision_is_invalidated_by_new_event(svc):
    seed_world(svc)
    _submit_and_accept(svc)
    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    svc.sign_off(docket_no="D1", role="business", signer="carol")
    assert svc.channel_status("S1")["channels"]["physical"]["state"] == "open"

    # 新的监管事件产生新版本，旧版本签署不再放行。
    svc.record_event(docket_no="D1", kind="correction_request",
                     occurred_at="2026-09-05T00:00:00", client_event_id="c1")
    channels = svc.channel_status("S1")["channels"]
    assert channels["physical"]["state"] == "restricted"
    assert channels["physical"]["reason"] == "correction"
