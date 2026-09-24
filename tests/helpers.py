"""测试用构造助手。"""

from __future__ import annotations


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def setup_license_and_rule(client, *, license_no="L-BJ-001", region="BJ",
                           expiry="2026-10-01T00:00:00", rule_version="v2026",
                           buffer_days=30, online_buffer_days=15, delivery_buffer_days=10,
                           required_documents=("application", "commitment"),
                           effective_from="2026-01-01T00:00:00",
                           buffer_continues_in_correction=True):
    response = client.post("/admin/licenses", json={
        "license_no": license_no,
        "region": region,
        "operator_id": "OP-1",
        "store_id": "ST-1",
        "current_expiry": expiry,
    })
    assert response.status_code == 201, response.get_json()
    response = client.post("/admin/region-rules", json={
        "region": region,
        "rule_version": rule_version,
        "effective_from": effective_from,
        "buffer_days": buffer_days,
        "online_buffer_days": online_buffer_days,
        "delivery_buffer_days": delivery_buffer_days,
        "required_documents": list(required_documents),
        "buffer_continues_in_correction": buffer_continues_in_correction,
    })
    assert response.status_code == 201, response.get_json()


def channel_map(status_payload) -> dict:
    return {c["channel"]: c for c in status_payload["channels"]}


def get_status(client, *, license_no="L-BJ-001", region="BJ", at):
    response = client.get(
        "/channels/status",
        query_string={"license_no": license_no, "region": region, "at": at},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()
