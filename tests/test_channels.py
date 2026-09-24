"""渠道裁定、缓冲到期、停服恢复续跑、暂停/转让、订单风险标记。"""

from tests.conftest import basic_material, seed_world, submit_and_accept


def test_channels_timeline_through_renewal(svc):
    seed_world(svc)
    # 旧证仍有效：全渠道开放，无需签署。
    status = svc.channel_status("S1", "2026-10-01T00:00:00")
    assert {c: status["channels"][c]["state"] for c in status["channels"]} == {
        "physical": "open", "online": "open", "delivery": "open",
    }

    submit_and_accept(svc, accepted_at="2026-10-01T00:00:00")
    in_grace = svc.channel_status("S1", "2026-10-10T00:00:00")["channels"]
    assert in_grace["physical"]["state"] == "open"
    assert in_grace["online"]["state"] == "restricted"
    assert in_grace["delivery"]["state"] == "restricted"

    # 默认规则受理后缓冲 30 天：第 31 天全部关闭。
    expired = svc.channel_status("S1", "2026-11-02T00:00:00")["channels"]
    assert all(expired[c]["state"] == "closed" for c in expired)
    assert expired["physical"]["reason"] == "grace_expired"


def test_due_tasks_run_after_outage_continue_deadlines(svc):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")

    # 缓冲到期与到期前通知任务已落库，且 due_at 是绝对时间。
    pending = {t["kind"] for t in svc.pending_tasks()}
    assert {"grace_expiry", "notify"} <= pending

    # 服务“停机”到缓冲到期之后才恢复：错过的任务一次性补齐，渠道关闭。
    result = svc.run_due_tasks("2026-10-15T00:00:00")
    assert result["count"] == 6  # 三渠道 × (到期前通知 + 缓冲到期)
    channels = svc.channel_status("S1", "2026-10-15T00:00:00")["channels"]
    assert all(channels[c]["state"] == "closed" for c in channels)
    assert svc.pending_tasks() == []


def test_correction_deadline_and_answer_cancel(svc):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    svc.record_event(docket_no="D1", kind="correction_request",
                     occurred_at="2026-09-05T00:00:00", client_event_id="c1")

    within = svc.channel_status("S1", "2026-09-10T00:00:00")["channels"]
    assert within["physical"]["state"] == "restricted"
    assert within["delivery"]["state"] == "closed"  # 配送在补正期直接关闭

    overdue = svc.channel_status("S1", "2026-09-30T00:00:00")["channels"]
    assert all(overdue[c]["state"] == "closed" for c in overdue)

    # 在期限内回复补正：补正任务取消；回复构成案卷新版本，需重新双签后放行。
    svc.record_event(docket_no="D1", kind="answered",
                     occurred_at="2026-09-08T00:00:00", client_event_id="c1-ans")
    refreshed = svc.channel_status("S1", "2026-09-09T00:00:00")["channels"]
    assert refreshed["physical"]["state"] == "restricted"
    assert refreshed["physical"]["reason"].startswith("awaiting_signoff")
    assert not any(t["kind"] == "correction_due" and t["status"] == "pending"
                   for t in svc.pending_tasks())

    svc.sign_off(docket_no="D1", role="compliance", signer="bob")
    svc.sign_off(docket_no="D1", role="business", signer="carol")
    assert svc.channel_status("S1", "2026-09-09T00:00:00")["channels"]["physical"]["state"] == "open"


def test_suspension_reevaluates_and_only_flags_orders(svc, fake_clock):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")

    # 缓冲开放期间成交一笔订单。
    order = svc.complete_order(order_no="O1", store_code="S1", channel="physical",
                               completed_at="2026-09-02T12:00:00")
    assert order["state"] == "open"

    # 9 月 4 日收到 9 月 3 日生效的暂停通知：立即重评未完成渠道。
    fake_clock.jump("2026-09-04T09:00:00")
    svc.record_license_action(license_number="L1", kind="suspended",
                              occurred_at="2026-09-03T00:00:00")
    assert svc.channel_status("S1")["channels"]["physical"]["state"] == "closed"

    detail = svc.get_order("O1")
    # 已成交订单不回滚，只追加风险标记；成交时快照仍为 open。
    assert detail["snapshot_state"] == "open"
    assert [f["kind"] for f in detail["risk_flags"]] == ["license_suspended"]

    # 恢复暂停后渠道按案卷状态重新开放。
    fake_clock.jump("2026-09-05T09:00:00")
    svc.record_license_action(license_number="L1", kind="reinstated",
                              occurred_at="2026-09-05T08:00:00")
    assert svc.channel_status("S1")["channels"]["physical"]["state"] == "open"


def test_operator_transfer_reevaluates_unfinished_channels(svc, fake_clock):
    seed_world(svc)
    svc.register_operator("OP2", "并购方医药")
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    assert svc.channel_status("S1")["channels"]["physical"]["state"] == "open"

    fake_clock.jump("2026-09-04T09:00:00")
    svc.record_license_action(
        license_number="L1", kind="transferred",
        occurred_at="2026-09-03T00:00:00",
        detail={"new_operator_code": "OP2", "reason": "主体转让"},
    )
    channels = svc.channel_status("S1")["channels"]
    assert all(channels[c]["state"] == "closed" for c in channels)
    assert channels["physical"]["reason"] == "license_transferred"


def test_new_license_issued_restores_full_open(svc):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    svc.record_event(
        docket_no="D1", kind="license_issued",
        occurred_at="2026-09-20T00:00:00", client_event_id="issue-1",
        payload={"new_expires_at": "2032-09-19T00:00:00"},
    )
    channels = svc.channel_status("S1", "2026-09-21T00:00:00")["channels"]
    assert all(channels[c]["state"] == "open" for c in channels)
    # 新证到达后缓冲任务全部取消。
    assert svc.pending_tasks() == []


def test_status_explains_basis_with_materials_rules_events(svc):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    report = svc.channel_status("S1", "2026-09-03T00:00:00")
    assert report["rule_version"] == "2026-1"
    ruling = report["persisted_rulings"]["physical"]
    assert ruling["materials"][0]["doc_key"] == "app_form"
    kinds = [e["kind"] for e in ruling["events"]]
    assert "submitted" in kinds and "accepted" in kinds

    physical = report["channels"]["physical"]
    assert physical["material_ids"] and physical["event_ids"]
    assert physical["deadlines"]["grace_end"] == "2026-10-01T00:00:00"


def test_no_renewal_after_expiry_closes_channels(svc):
    seed_world(svc, expires_at="2026-08-31T00:00:00")
    channels = svc.channel_status("S1", "2026-09-02T00:00:00")["channels"]
    assert all(channels[c]["state"] == "closed" for c in channels)
    assert channels["physical"]["reason"] == "no_renewal_after_expiry"


def test_historical_ruling_keeps_frozen_material_snapshot(svc, fake_clock):
    seed_world(svc)
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    ruling_at = "2026-09-03T00:00:00"
    first = svc.channel_status("S1", ruling_at)["persisted_rulings"]["physical"]
    assert first["materials"][0]["content_hash"] == "h1"

    # 之后同一文件内容变化进入核查：历史裁定的材料快照仍是原内容。
    fake_clock.jump("2026-09-10T00:00:00")
    svc.add_materials("D1", [{
        "doc_key": "app_form", "source_summary": "重新提交件",
        "content_hash": "h999", "valid_from": "2026-09-09T00:00:00",
    }])
    historical = svc.channel_status("S1", ruling_at)["persisted_rulings"]["physical"]
    assert historical["materials"][0]["content_hash"] == "h1"


def test_new_certificate_period_as_of_replay(svc, fake_clock):
    seed_world(svc, expires_at="2026-09-30T00:00:00")
    submit_and_accept(svc, accepted_at="2026-09-01T00:00:00")
    before_issue = "2026-09-20T00:00:00"
    # 发证前缓冲期：受理+双签下实体店开放。
    assert svc.channel_status("S1", before_issue)["channels"]["physical"]["state"] == "open"

    fake_clock.jump("2026-09-25T00:00:00")
    svc.record_event(
        docket_no="D1", kind="license_issued",
        occurred_at="2026-09-24T00:00:00", client_event_id="issue",
        payload={"new_expires_at": "2032-09-23T00:00:00"},
    )
    # 发证前日期的解释不被新证倒改；发证后新证有效期生效。
    assert svc.channel_status("S1", before_issue)["channels"]["physical"]["stage"] == "accepted"
    after = svc.channel_status("S1", "2027-01-01T00:00:00")["channels"]
    assert all(after[c]["state"] == "open" for c in after)
    assert after["physical"]["stage"] == "valid_license"
