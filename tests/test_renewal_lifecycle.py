"""续办案卷与缓冲放行的端到端生命周期。"""

from tests.helpers import channel_map, get_status, setup_license_and_rule


def _open_case(client, case_ref="RC-1", initiated_by="alice"):
    response = client.post("/renewal-cases", json={
        "license_no": "L-BJ-001",
        "region": "BJ",
        "operator_id": "OP-1",
        "store_id": "ST-1",
        "initiated_by": initiated_by,
        "case_ref": case_ref,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _submit(client, materials, at, case_ref="RC-1", note=""):
    response = client.post(f"/renewal-cases/{case_ref}/batches", json={
        "materials": materials, "at": at, "note": note,
    })
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def test_material_resend_reuses_record_and_change_enters_verification(client):
    setup_license_and_rule(client)
    _open_case(client)
    first = _submit(client, [
        {"document_key": "application", "source_summary": "申请表v1",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书v1",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-20T00:00:00")
    assert first["version_seq"] == 1

    # 完全相同文件重送：沿用原记录，不产生新材料行。
    # 同一标识内容变化：进入核查（新版本）。
    second = _submit(client, [
        {"document_key": "application", "source_summary": "申请表v1",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书内容已变更",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-21T00:00:00")
    assert second["version_seq"] == 2
    assert "application" in second["reused"]
    assert second["new_or_changed"] == ["commitment"]
    # 沿用的材料 id 与首批一致；变化材料产生新 id。
    first_ids = dict(zip(["application", "commitment"], first["material_ids"]))
    second_ids = dict(zip(["application", "commitment"], second["material_ids"]))
    assert second_ids["application"] == first_ids["application"]
    assert second_ids["commitment"] != first_ids["commitment"]


def test_dual_signoff_must_use_same_version_and_initiator_cannot_sign(client):
    setup_license_and_rule(client)
    _open_case(client, initiated_by="alice")
    _submit(client, [
        {"document_key": "application", "source_summary": "申请表v1",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书v1",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-20T00:00:00")

    # 发起人不能自批。
    response = client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "alice", "at": "2026-09-20T12:00:00",
    })
    assert response.status_code == 403
    assert response.get_json()["error"] == "self_approval_forbidden"

    # 合规签署版本 1。
    response = client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "carol", "at": "2026-09-20T12:00:00",
    })
    assert response.status_code == 201
    # 同角色不能重复签署同一版本。
    response = client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "erin", "at": "2026-09-20T13:00:00",
    })
    assert response.status_code == 409

    # 案卷升版后，旧版本签署不再构成放行；业务负责人对版本 2 的签署
    # 不能与合规对版本 1 的签署凑成双签。
    _submit(client, [
        {"document_key": "commitment", "source_summary": "承诺书v2",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-22T00:00:00")
    response = client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "business", "signer": "dave", "at": "2026-09-22T12:00:00",
    })
    assert response.status_code == 201
    assert response.get_json()["case_version_seq"] == 2

    # 核查通过 + 受理，使材料闸门与受理前提满足；此时仅 v2 单签，缓冲内受限。
    for key in ("application", "commitment"):
        response = client.post("/renewal-cases/RC-1/material-checks", json={
            "document_key": key, "result": "verified", "decided_by": "auditor",
            "at": "2026-09-23T00:00:00",
        })
        assert response.status_code == 200
    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "accepted", "occurred_at": "2026-09-25T00:00:00",
        "recorded_at": "2026-09-25T10:00:00",
    })
    assert response.status_code == 201

    status = get_status(client, at="2026-10-05T12:00:00")
    physical = channel_map(status)["physical"]
    assert physical["state"] == "restricted"
    assert physical["reason_code"] == "awaiting_dual_signoff"
    assert physical["case_version_seq"] == 2

    # 合规对版本 2 补签后，双签齐备，缓冲放行。
    response = client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "carol", "at": "2026-09-26T00:00:00",
    })
    assert response.status_code == 201
    status = get_status(client, at="2026-10-05T12:00:00")
    physical = channel_map(status)["physical"]
    assert physical["state"] == "open"
    assert physical["reason_code"] == "buffer_released"
    # 解释中必须列出所采用的材料、事件与规则版本。
    assert physical["rule_version"] == "v2026"
    used_keys = {m["document_key"] for m in physical["materials_used"]}
    assert {"application", "commitment"} <= used_keys
    assert {e["event_type"] for e in physical["events_used"]} == {"accepted"}


def test_channel_specific_buffers_expire_independently(client):
    setup_license_and_rule(client)
    _open_case(client)
    _submit(client, [
        {"document_key": "application", "source_summary": "申请表",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-20T00:00:00")
    client.post("/renewal-cases/RC-1/material-checks", json={
        "document_key": "application", "result": "verified",
        "decided_by": "auditor", "at": "2026-09-20T00:00:00"})
    client.post("/renewal-cases/RC-1/material-checks", json={
        "document_key": "commitment", "result": "verified",
        "decided_by": "auditor", "at": "2026-09-20T00:00:00"})
    client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "accepted", "occurred_at": "2026-09-25T00:00:00",
        "recorded_at": "2026-09-25T10:00:00"})
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "carol", "at": "2026-09-26T00:00:00"})
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "business", "signer": "dave", "at": "2026-09-26T00:00:00"})

    # 旧证 10-01 截止；缓冲：实体 30 天 / 线上 15 天 / 配送 10 天，
    # 到期日当天即关闭（10-31 / 10-16 / 10-11）。
    cases = {
        "2026-09-30T12:00:00": {"physical": "open", "online": "open", "delivery": "open"},
        "2026-10-12T12:00:00": {"physical": "open", "online": "open", "delivery": "closed"},
        "2026-10-18T12:00:00": {"physical": "open", "online": "closed", "delivery": "closed"},
        "2026-11-01T12:00:00": {"physical": "closed", "online": "closed", "delivery": "closed"},
    }
    for at, expected in cases.items():
        channels = channel_map(get_status(client, at=at))
        for channel, state in expected.items():
            assert channels[channel]["state"] == state, (at, channel)
        if at == "2026-10-12T12:00:00":
            assert channels["delivery"]["reason_code"] == "buffer_expired"


def test_no_acceptance_before_expiry_closes_channels(client):
    setup_license_and_rule(client)
    _open_case(client)
    _submit(client, [
        {"document_key": "application", "source_summary": "申请表",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-20T00:00:00")
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "carol", "at": "2026-09-26T00:00:00"})
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "business", "signer": "dave", "at": "2026-09-26T00:00:00"})
    # 直到旧证截止仍无受理回执：双签也不能放行。
    channels = channel_map(get_status(client, at="2026-10-02T00:00:00"))
    for channel in channels.values():
        assert channel["state"] == "closed"
        assert channel["reason_code"] == "no_acceptance_before_expiry"


def test_approval_restricted_until_license_arrives_then_open(client):
    setup_license_and_rule(client, buffer_days=60, online_buffer_days=60, delivery_buffer_days=60)
    _open_case(client)
    _submit(client, [
        {"document_key": "application", "source_summary": "申请表",
         "valid_from": "2026-09-01T00:00:00"},
        {"document_key": "commitment", "source_summary": "承诺书",
         "valid_from": "2026-09-01T00:00:00"},
    ], at="2026-09-20T00:00:00")
    for key in ("application", "commitment"):
        client.post("/renewal-cases/RC-1/material-checks", json={
            "document_key": key, "result": "verified",
            "decided_by": "auditor", "at": "2026-09-20T00:00:00"})
    client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "accepted", "occurred_at": "2026-09-25T00:00:00",
        "recorded_at": "2026-09-25T10:00:00"})
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "compliance", "signer": "carol", "at": "2026-09-26T00:00:00"})
    client.post("/renewal-cases/RC-1/signoffs", json={
        "role": "business", "signer": "dave", "at": "2026-09-26T00:00:00"})
    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "approved", "occurred_at": "2026-10-20T00:00:00",
        "recorded_at": "2026-10-20T10:00:00"})
    assert response.status_code == 201

    channels = channel_map(get_status(client, at="2026-10-25T00:00:00"))
    for channel in channels.values():
        assert channel["state"] == "restricted"
        assert channel["reason_code"] == "approved_awaiting_license"

    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "licensed", "occurred_at": "2026-10-28T00:00:00",
        "recorded_at": "2026-10-28T09:00:00",
        "detail": {"new_expiry": "2031-10-01T00:00:00"}})
    assert response.status_code == 201
    channels = channel_map(get_status(client, at="2026-10-29T00:00:00"))
    for channel in channels.values():
        assert channel["state"] == "open"
        assert channel["reason_code"] == "licensed_new_certificate"

    # 新证有效期写回：次年仍以新证开放。
    channels = channel_map(get_status(client, at="2027-10-01T00:00:00"))
    assert all(c["state"] == "open" for c in channels.values())


def test_two_renewals_contesting_license_have_single_effective_case(client):
    setup_license_and_rule(client)
    _open_case(client, case_ref="RC-FIRST")
    response = client.post("/renewal-cases", json={
        "license_no": "L-BJ-001", "region": "BJ", "operator_id": "OP-1",
        "store_id": "ST-1", "initiated_by": "bob", "case_ref": "RC-SECOND",
    })
    assert response.status_code == 409
    assert response.get_json()["error"] == "case_conflict"

    # 时点解释采用的也是唯一生效案卷 RC-FIRST。
    status = get_status(client, at="2026-09-30T00:00:00")
    assert status["case"]["case_ref"] == "RC-FIRST"
