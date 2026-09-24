"""HTTP 边界端到端：真实 JSON 请求走完续办到渠道放行/关闭全流程。"""


def _seed(client):
    assert client.post("/admin/regions", json={"code": "BJ", "name": "北京"}).status_code == 201
    assert client.post("/admin/rule-versions", json={
        "region_code": "BJ", "version": "2026-1",
        "effective_from": "2026-01-01T00:00:00",
    }).status_code == 201
    assert client.post("/admin/operators", json={
        "code": "OP1", "name": "回春堂"}).status_code == 201
    assert client.post("/admin/stores", json={
        "code": "S1", "name": "一号店", "region_code": "BJ",
        "operator_code": "OP1"}).status_code == 201
    assert client.post("/admin/licenses", json={
        "number": "L1", "store_code": "S1", "operator_code": "OP1",
        "issued_at": "2025-01-01T00:00:00",
        "expires_at": "2027-06-30T00:00:00"}).status_code == 201


def test_full_renewal_flow_over_http(client):
    _seed(client)

    # 提交续办批次。
    resp = client.post("/renewals/batches", json={
        "docket_no": "D1", "store_code": "S1", "license_number": "L1",
        "operator_code": "OP1", "created_by": "alice",
        "materials": [{
            "doc_key": "app_form",
            "source_summary": "门店柜台原件扫描",
            "content_hash": "h1",
            "valid_from": "2026-08-01T00:00:00",
        }],
    })
    assert resp.status_code == 201

    # 同批次重发幂等。
    again = client.post("/renewals/batches", json={
        "docket_no": "D1", "store_code": "S1", "license_number": "L1",
        "operator_code": "OP1", "created_by": "alice",
        "materials": [],
    })
    assert again.status_code == 200 and again.get_json()["deduplicated"] is True

    # 监管受理。
    assert client.post("/renewals/D1/events", json={
        "kind": "accepted", "occurred_at": "2026-09-01T00:00:00",
        "client_event_id": "rcpt-1",
    }).status_code == 201

    # 发起人自批被拒绝（422）。
    forbidden = client.post("/renewals/D1/signoffs", json={
        "role": "compliance", "signer": "alice"})
    assert forbidden.status_code == 422

    # 合规与业务分别签署。
    assert client.post("/renewals/D1/signoffs", json={
        "role": "compliance", "signer": "bob"}).status_code == 201
    assert client.post("/renewals/D1/signoffs", json={
        "role": "business", "signer": "carol"}).status_code == 201

    report = client.get("/stores/S1/channels").get_json()
    assert report["channels"]["physical"]["state"] == "open"
    assert report["rule_version"] == "2026-1"
    assert report["persisted_rulings"]["physical"]["materials"][0]["doc_key"] == "app_form"

    # 同一回执重复送达：幂等。
    dup = client.post("/renewals/D1/events", json={
        "kind": "accepted", "occurred_at": "2026-09-01T00:00:00",
        "client_event_id": "rcpt-1",
    })
    assert dup.status_code == 200 and dup.get_json()["deduplicated"] is True


def test_suspension_closes_channel_and_flags_order(client):
    _seed(client)
    client.post("/renewals/batches", json={
        "docket_no": "D1", "store_code": "S1", "license_number": "L1",
        "operator_code": "OP1", "created_by": "alice",
        "materials": [{
            "doc_key": "m", "source_summary": "s", "content_hash": "h",
            "valid_from": "2026-08-01T00:00:00",
        }],
    })
    client.post("/renewals/D1/events", json={
        "kind": "accepted", "occurred_at": "2026-09-01T00:00:00",
        "client_event_id": "a1"})
    client.post("/renewals/D1/signoffs", json={"role": "compliance", "signer": "bob"})
    client.post("/renewals/D1/signoffs", json={"role": "business", "signer": "carol"})

    order = client.post("/orders", json={
        "order_no": "O1", "store_code": "S1", "channel": "physical",
        "completed_at": "2026-09-02T00:00:00",
    }).get_json()
    assert order["state"] == "open"

    resp = client.post("/licenses/L1/actions", json={
        "kind": "suspended", "occurred_at": "2026-09-03T00:00:00"})
    assert resp.status_code == 201

    assert client.get("/stores/S1/channels").get_json()["channels"]["physical"]["state"] == "closed"
    detail = client.get("/orders/O1").get_json()
    assert detail["snapshot_state"] == "open"
    assert [f["kind"] for f in detail["risk_flags"]] == ["license_suspended"]


def test_due_tasks_endpoint_closes_after_outage(client):
    _seed(client)
    client.post("/renewals/batches", json={
        "docket_no": "D1", "store_code": "S1", "license_number": "L1",
        "operator_code": "OP1", "created_by": "alice",
        "materials": [{
            "doc_key": "m", "source_summary": "s", "content_hash": "h",
            "valid_from": "2026-08-01T00:00:00",
        }],
    })
    client.post("/renewals/D1/events", json={
        "kind": "accepted", "occurred_at": "2026-09-01T00:00:00",
        "client_event_id": "a1"})
    client.post("/renewals/D1/signoffs", json={"role": "compliance", "signer": "bob"})
    client.post("/renewals/D1/signoffs", json={"role": "business", "signer": "carol"})

    pending = client.get("/tasks/pending").get_json()["tasks"]
    assert {t["kind"] for t in pending} == {"grace_expiry", "notify"}

    result = client.post("/tasks/run-due", json={"as_of": "2026-10-15T00:00:00"}).get_json()
    assert result["count"] == 6
    channels = client.get("/stores/S1/channels",
                          query_string={"as_of": "2026-10-15T00:00:00"}).get_json()
    assert all(channels["channels"][c]["state"] == "closed" for c in channels["channels"])


def test_health_still_ok(client):
    assert client.get("/health").get_json() == {"status": "ok", "storage": "sqlite"}
