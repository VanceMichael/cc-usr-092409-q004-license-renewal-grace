"""迟到回执、暂停/转让、停服恢复与追加审计的端到端测试。"""

from sqlalchemy import select

from pharmacy_identity.models import (
    ChannelSnapshot,
    LicenseEvent,
    MaintenanceTask,
    OrderRiskMarker,
    RegulatoryEvent,
)
from tests.helpers import channel_map, get_status, setup_license_and_rule


def _seed_open_case_with_acceptance(client, case_ref="RC-1", *, at_acceptance="2026-09-25T00:00:00",
                                    sign=True, verified=True):
    response = client.post("/renewal-cases", json={
        "license_no": "L-BJ-001", "region": "BJ", "operator_id": "OP-1",
        "store_id": "ST-1", "initiated_by": "alice", "case_ref": case_ref,
    })
    assert response.status_code == 201, response.get_json()
    response = client.post(f"/renewal-cases/{case_ref}/batches", json={
        "materials": [
            {"document_key": "application", "source_summary": "申请表",
             "valid_from": "2026-09-01T00:00:00"},
            {"document_key": "commitment", "source_summary": "承诺书",
             "valid_from": "2026-09-01T00:00:00"},
        ],
        "at": "2026-09-20T00:00:00",
    })
    assert response.status_code == 201
    if verified:
        for key in ("application", "commitment"):
            assert client.post(f"/renewal-cases/{case_ref}/material-checks", json={
                "document_key": key, "result": "verified",
                "decided_by": "auditor", "at": "2026-09-20T00:00:00",
            }).status_code == 200
    assert client.post(f"/renewal-cases/{case_ref}/regulatory-events", json={
        "event_type": "accepted", "occurred_at": at_acceptance,
        "recorded_at": "2026-09-25T10:00:00",
    }).status_code == 201
    if sign:
        assert client.post(f"/renewal-cases/{case_ref}/signoffs", json={
            "role": "compliance", "signer": "carol", "at": "2026-09-26T00:00:00",
        }).status_code == 201
        assert client.post(f"/renewal-cases/{case_ref}/signoffs", json={
            "role": "business", "signer": "dave", "at": "2026-09-26T00:00:00",
        }).status_code == 201


def test_late_acceptance_after_rejection_does_not_rewrite_decision(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client)
    # 驳回发生于 10-05 并当日录入。
    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "rejected", "occurred_at": "2026-10-05T00:00:00",
        "recorded_at": "2026-10-05T09:00:00",
        "detail": {"reason": "主体材料不一致"},
    })
    assert response.status_code == 201
    assert get_status(client, at="2026-10-06T00:00:00")["channels"][0]["reason_code"] == "renewal_rejected"

    # 一份受理回执迟到：occurred_at=09-24，却在 10-08 才录入。
    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "accepted", "occurred_at": "2026-09-24T00:00:00",
        "recorded_at": "2026-10-08T00:00:00",
    })
    assert response.status_code == 201
    assert response.get_json()["is_late"] is True

    # 驳回结论不倒改：10-09 仍为因驳回关闭。
    channels = channel_map(get_status(client, at="2026-10-09T00:00:00"))
    assert all(c["state"] == "closed" for c in channels.values())
    assert all(c["reason_code"] == "renewal_rejected" for c in channels.values())
    # 历史日期解释不变。
    assert (get_status(client, at="2026-10-06T12:00:00")["channels"][0]["reason_code"]
            == "renewal_rejected")


def test_suspension_closes_all_channels_and_marks_completed_orders(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client)
    # 缓冲放行中：10-05 实体/线上/配送（10 天内）开放。
    channels = channel_map(get_status(client, at="2026-10-05T08:00:00"))
    assert channels["physical"]["state"] == "open"

    response = client.post("/licenses/suspensions", json={
        "license_no": "L-BJ-001", "region": "BJ",
        "occurred_at": "2026-10-05T09:00:00", "reason": "飞行检查发现风险",
        "completed_orders": {"online": ["ORD-1001", "ORD-1001"], "delivery": ["ORD-1002"]},
    })
    assert response.status_code == 201
    channels = channel_map(get_status(client, at="2026-10-05T10:00:00"))
    assert all(c["state"] == "closed" for c in channels.values())
    assert all(c["reason_code"] == "license_suspended" for c in channels.values())

    # 已成交订单只追加风险标记（去重后 2 条），订单本身不被改动。
    with client.application.app_context():
        from sqlalchemy.orm import Session
        session = Session(client.application.config["ENGINE"])
        markers = session.scalars(select(OrderRiskMarker).order_by(OrderRiskMarker.order_no)).all()
        assert [m.order_no for m in markers] == ["ORD-1001", "ORD-1002"]
        assert all(m.risk_code == "license_suspended_order_risk" for m in markers)
        # 许可证书事件为追加流。
        assert session.scalar(
            select(LicenseEvent.event_type).where(LicenseEvent.event_type == "suspended")
        )
        session.close()

    # 恢复后续办案卷仍生效，按缓冲规则继续判定。
    assert client.post("/licenses/resumptions", json={
        "license_no": "L-BJ-001", "region": "BJ",
        "occurred_at": "2026-10-06T00:00:00",
    }).status_code == 201
    channels = channel_map(get_status(client, at="2026-10-06T12:00:00"))
    assert channels["physical"]["state"] == "open"
    # 配送 10 天缓冲 10-11 到期：恢复后到 10-12 已关闭，实体仍开放。
    channels = channel_map(get_status(client, at="2026-10-12T12:00:00"))
    assert channels["physical"]["state"] == "open"
    assert channels["delivery"]["state"] == "closed"
    assert channels["delivery"]["reason_code"] == "buffer_expired"


def test_transfer_supersedes_case_and_closes_for_old_operator(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client)
    response = client.post("/licenses/transfers", json={
        "license_no": "L-BJ-001", "region": "BJ", "new_operator_id": "OP-2",
        "occurred_at": "2026-10-05T00:00:00",
        "completed_orders": {"physical": ["ORD-2001"]},
    })
    assert response.status_code == 201
    status = get_status(client, at="2026-10-05T12:00:00")
    assert all(c["state"] == "closed" for c in status["channels"])
    assert status["case"]["status"] == "superseded"

    # 待办任务随转让取消，且已成交订单只加标记。
    with client.application.app_context():
        from sqlalchemy.orm import Session
        session = Session(client.application.config["ENGINE"])
        pending = session.scalars(
            select(MaintenanceTask).where(MaintenanceTask.status == "pending")
        ).all()
        assert pending == []
        marker = session.scalar(select(OrderRiskMarker))
        assert marker.order_no == "ORD-2001"
        assert marker.risk_code == "license_transferred_order_risk"
        session.close()


def test_correction_deadline_closes_after_due(client):
    # 补正期间允许继续缓冲。
    setup_license_and_rule(client, buffer_days=30, online_buffer_days=30, delivery_buffer_days=30)
    _seed_open_case_with_acceptance(client)
    response = client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "correction", "occurred_at": "2026-10-08T00:00:00",
        "recorded_at": "2026-10-08T10:00:00",
        "detail": {"deadline_at": "2026-10-15T00:00:00", "items": ["承诺书补章"]},
    })
    assert response.status_code == 201
    channels = channel_map(get_status(client, at="2026-10-10T00:00:00"))
    assert channels["physical"]["state"] == "open"
    assert channels["physical"]["reason_code"] == "buffer_released"

    channels = channel_map(get_status(client, at="2026-10-16T00:00:00"))
    assert all(c["state"] == "closed" for c in channels.values())
    assert all(c["reason_code"] == "correction_overdue" for c in channels.values())

    # 补正完成后再次受理，缓冲恢复。
    assert client.post("/renewal-cases/RC-1/regulatory-events", json={
        "event_type": "accepted", "occurred_at": "2026-10-18T00:00:00",
        "recorded_at": "2026-10-18T10:00:00",
    }).status_code == 201
    channels = channel_map(get_status(client, at="2026-10-19T00:00:00"))
    assert channels["physical"]["state"] == "open"


def test_outage_recovery_continues_buffer_expiry_and_notification(client):
    setup_license_and_rule(client, buffer_days=5, online_buffer_days=5, delivery_buffer_days=5)
    _seed_open_case_with_acceptance(client)
    # 旧证 10-01 截止，缓冲 10-06 到期。服务在 10-02 至 10-10 停服。
    response = client.post("/maintenance/run-due-tasks", json={"now": "2026-10-10T00:00:00"})
    assert response.status_code == 200
    processed = response.get_json()["processed"]
    kinds = {(p["task_type"], p["channel"]) for p in processed}
    assert ("buffer_expiry", "physical") in kinds
    notifications = [n for p in processed for n in p["notifications"]]
    notified_channels = {c for n in notifications for c in n["channels"]}
    assert {"physical", "online", "delivery"} <= notified_channels

    # 到期任务只按原墙钟时点（10-06）重评一次，不重复执行。
    second = client.post("/maintenance/run-due-tasks", json={"now": "2026-10-11T00:00:00"})
    assert second.get_json()["processed"] == []
    channels = channel_map(get_status(client, at="2026-10-10T00:00:00"))
    assert all(c["state"] == "closed" for c in channels.values())
    assert all(c["reason_code"] == "buffer_expired" for c in channels.values())


def test_snapshots_and_events_are_append_only(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client)
    # 触发若干状态变迁。
    client.post("/licenses/suspensions", json={
        "license_no": "L-BJ-001", "region": "BJ",
        "occurred_at": "2026-10-05T09:00:00"})
    client.post("/licenses/resumptions", json={
        "license_no": "L-BJ-001", "region": "BJ",
        "occurred_at": "2026-10-06T00:00:00"})

    engine = client.application.config["ENGINE"]
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        events = session.scalars(
            select(RegulatoryEvent).order_by(RegulatoryEvent.seq)
        ).all()
        assert [e.event_type for e in events] == ["accepted"]
        snapshots = session.scalars(
            select(ChannelSnapshot).order_by(
                ChannelSnapshot.channel, ChannelSnapshot.valid_from
            )
        ).all()
        # 每个渠道都留下了 open -> closed(suspended) 等多条追加快照；
        # 不存在原地覆盖（同渠道 valid_from 序列非递减且 trigger 可追溯）。
        by_channel = {}
        for snap in snapshots:
            by_channel.setdefault(snap.channel, []).append(snap)
        assert set(by_channel) == {"physical", "online", "delivery"}
        for rows in by_channel.values():
            triggers = {r.trigger for r in rows}
            assert "license_suspended" in triggers
            valids = [r.valid_from for r in rows]
            assert valids == sorted(valids)


def test_status_explanation_references_materials_rule_and_events(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client)
    status = get_status(client, at="2026-10-05T12:00:00")
    assert status["rule"]["rule_version"] == "v2026"
    physical = channel_map(status)["physical"]
    assert physical["state"] == "open"
    assert physical["rule_version"] == "v2026"
    assert physical["case_version_seq"] == 1
    summaries = {m["source_summary"] for m in physical["materials_used"]}
    assert {"申请表", "承诺书"} <= summaries
    assert [e["event_type"] for e in physical["events_used"]] == ["accepted"]


def test_rejected_case_frees_license_for_new_renewal(client):
    setup_license_and_rule(client)
    _seed_open_case_with_acceptance(client, case_ref="RC-OLD")
    assert client.post("/renewal-cases/RC-OLD/regulatory-events", json={
        "event_type": "rejected", "occurred_at": "2026-10-05T00:00:00",
        "recorded_at": "2026-10-05T09:00:00",
    }).status_code == 201
    response = client.post("/renewal-cases", json={
        "license_no": "L-BJ-001", "region": "BJ", "operator_id": "OP-1",
        "store_id": "ST-1", "initiated_by": "alice", "case_ref": "RC-NEW",
    })
    assert response.status_code == 201
    status = get_status(client, at="2026-10-07T00:00:00")
    assert status["case"]["case_ref"] == "RC-NEW"
