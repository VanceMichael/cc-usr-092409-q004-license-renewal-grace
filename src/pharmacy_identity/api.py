"""HTTP 边界：把请求 JSON 交给领域服务，统一错误映射与会话提交。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy.orm import Session

from .services import DomainError, IdentityService


def create_api_blueprint(engine) -> Blueprint:
    api = Blueprint("api", __name__)

    def service() -> IdentityService:
        # 每个请求一个会话；视图正常返回后由 after_this_request 提交。
        session = Session(engine)
        request._db_session = session
        return IdentityService(session)

    @api.after_request
    def _commit(response):
        session = getattr(request, "_db_session", None)
        if session is not None:
            if response.status_code < 400:
                session.commit()
            else:
                session.rollback()
            session.close()
        return response

    @api.errorhandler(DomainError)
    def _domain_error(error: DomainError):
        return jsonify(error=error.code, message=error.message), error.status

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise DomainError("bad_json", "请求体必须是 JSON 对象", 400)
        return data

    def require(data: dict, *keys: str) -> None:
        missing = [key for key in keys if data.get(key) in (None, "")]
        if missing:
            raise DomainError("missing_fields", f"缺少必填字段：{','.join(missing)}", 400)

    def iso(value):
        return value  # 服务层的 as_utc 接受 ISO 字符串

    # --------------------------------------------------------- 基础数据

    @api.post("/admin/licenses")
    def register_license():
        data = body()
        require(data, "license_no", "region", "operator_id", "store_id", "current_expiry")
        svc = service()
        row = svc.register_license(
            license_no=data["license_no"],
            region=data["region"],
            operator_id=data["operator_id"],
            store_id=data["store_id"],
            current_expiry=iso(data["current_expiry"]),
        )
        return jsonify(
            id=row.id,
            license_no=row.license_no,
            region=row.region,
            operator_id=row.operator_id,
            store_id=row.store_id,
            current_expiry=row.current_expiry.isoformat(),
        ), 201

    @api.post("/admin/region-rules")
    def register_rule():
        data = body()
        require(data, "region", "rule_version", "effective_from")
        svc = service()
        row = svc.register_rule(
            region=data["region"],
            rule_version=data["rule_version"],
            effective_from=iso(data["effective_from"]),
            buffer_days=data.get("buffer_days", 0),
            online_buffer_days=data.get("online_buffer_days"),
            delivery_buffer_days=data.get("delivery_buffer_days"),
            buffer_requires_acceptance=data.get("buffer_requires_acceptance", True),
            buffer_continues_in_correction=data.get("buffer_continues_in_correction", True),
            required_documents=data.get("required_documents"),
            detail=data.get("detail"),
        )
        return jsonify(
            id=row.id,
            region=row.region,
            rule_version=row.rule_version,
            effective_from=row.effective_from.isoformat(),
        ), 201

    # --------------------------------------------------------- 案卷 / 材料

    @api.post("/renewal-cases")
    def open_case():
        data = body()
        require(data, "license_no", "region", "operator_id", "store_id", "initiated_by")
        svc = service()
        row = svc.open_case(
            license_no=data["license_no"],
            region=data["region"],
            operator_id=data["operator_id"],
            store_id=data["store_id"],
            initiated_by=data["initiated_by"],
            case_ref=data.get("case_ref"),
        )
        return jsonify(
            case_ref=row.case_ref,
            license_no=row.license_no,
            region=row.region,
            operator_id=row.operator_id,
            store_id=row.store_id,
            status=row.status,
        ), 201

    @api.post("/renewal-cases/<case_ref>/batches")
    def submit_batch(case_ref: str):
        data = body()
        require(data, "materials")
        if not isinstance(data["materials"], list) or not data["materials"]:
            raise DomainError("bad_materials", "materials 必须是非空数组")
        for item in data["materials"]:
            require(item, "document_key", "source_summary", "valid_from")
        svc = service()
        result = svc.submit_batch(
            case_ref,
            data["materials"],
            note=data.get("note", ""),
            at=iso(data["at"]) if data.get("at") else None,
        )
        return jsonify(result), 201

    @api.post("/renewal-cases/<case_ref>/material-checks")
    def material_check(case_ref: str):
        data = body()
        require(data, "result", "decided_by")
        if not data.get("document_key") and not data.get("material_revision_id"):
            raise DomainError(
                "missing_fields", "document_key 与 material_revision_id 至少提供一个"
            )
        svc = service()
        row = svc.resolve_material_check(
            case_ref,
            document_key=data.get("document_key"),
            material_revision_id=data.get("material_revision_id"),
            result=data["result"],
            decided_by=data["decided_by"],
            at=iso(data["at"]) if data.get("at") else None,
            comment=data.get("comment", ""),
        )
        return jsonify(
            material_revision_id=row.id,
            document_key=row.document_key,
            revision=row.revision,
            verification=row.verification,
        )

    # --------------------------------------------------------- 监管事件

    @api.post("/renewal-cases/<case_ref>/regulatory-events")
    def regulatory_event(case_ref: str):
        data = body()
        require(data, "event_type", "occurred_at")
        svc = service()
        row = svc.add_regulatory_event(
            case_ref,
            data["event_type"],
            occurred_at=iso(data["occurred_at"]),
            detail=data.get("detail"),
            recorded_at=iso(data["recorded_at"]) if data.get("recorded_at") else None,
        )
        return jsonify(
            id=row.id,
            seq=row.seq,
            event_type=row.event_type,
            occurred_at=row.occurred_at.isoformat(),
            recorded_at=row.recorded_at.isoformat(),
            is_late=row.is_late,
        ), 201

    # --------------------------------------------------------- 双签

    @api.post("/renewal-cases/<case_ref>/signoffs")
    def signoff(case_ref: str):
        data = body()
        require(data, "role", "signer")
        svc = service()
        row = svc.sign_buffer(
            case_ref,
            role=data["role"],
            signer=data["signer"],
            channels=data.get("channels"),
            at=iso(data["at"]) if data.get("at") else None,
        )
        return jsonify(
            id=row.id,
            role=row.role,
            signer=row.signer,
            case_version_seq=row.case_version_seq,
            channels=row.channels,
            signed_at=row.signed_at.isoformat(),
        ), 201

    # ------------------------------------------------- 暂停 / 恢复 / 转让

    @api.post("/licenses/suspensions")
    def suspend():
        data = body()
        require(data, "license_no", "region")
        svc = service()
        event = svc.suspend_license(
            license_no=data["license_no"],
            region=data["region"],
            occurred_at=iso(data["occurred_at"]) if data.get("occurred_at") else None,
            reason=data.get("reason", ""),
            completed_orders=data.get("completed_orders"),
        )
        return jsonify(
            id=event.id,
            seq=event.seq,
            event_type=event.event_type,
            occurred_at=event.occurred_at.isoformat(),
        ), 201

    @api.post("/licenses/resumptions")
    def resume():
        data = body()
        require(data, "license_no", "region")
        svc = service()
        event = svc.resume_license(
            license_no=data["license_no"],
            region=data["region"],
            occurred_at=iso(data["occurred_at"]) if data.get("occurred_at") else None,
            reason=data.get("reason", ""),
        )
        return jsonify(
            id=event.id,
            seq=event.seq,
            event_type=event.event_type,
            occurred_at=event.occurred_at.isoformat(),
        ), 201

    @api.post("/licenses/transfers")
    def transfer():
        data = body()
        require(data, "license_no", "region", "new_operator_id")
        svc = service()
        event = svc.transfer_license(
            license_no=data["license_no"],
            region=data["region"],
            new_operator_id=data["new_operator_id"],
            occurred_at=iso(data["occurred_at"]) if data.get("occurred_at") else None,
            reason=data.get("reason", ""),
            completed_orders=data.get("completed_orders"),
        )
        return jsonify(
            id=event.id,
            seq=event.seq,
            event_type=event.event_type,
            occurred_at=event.occurred_at.isoformat(),
        ), 201

    # --------------------------------------------------------- 订单风险标记

    @api.post("/orders/risk-markers")
    def risk_markers():
        data = body()
        require(data, "license_no", "region", "orders", "risk_code")
        if not isinstance(data["orders"], list):
            raise DomainError("bad_orders", "orders 必须是数组")
        for order in data["orders"]:
            require(order, "order_no", "channel")
        svc = service()
        rows = svc.tag_orders(
            license_no=data["license_no"],
            region=data["region"],
            orders=data["orders"],
            risk_code=data["risk_code"],
            at=iso(data["at"]) if data.get("at") else None,
        )
        return jsonify(
            markers=[
                {
                    "id": row.id,
                    "order_no": row.order_no,
                    "channel": row.channel,
                    "risk_code": row.risk_code,
                    "marked_at": row.marked_at.isoformat(),
                }
                for row in rows
            ]
        ), 201

    # --------------------------------------------------------- 停服恢复

    @api.post("/maintenance/run-due-tasks")
    def run_due_tasks():
        data = body()
        svc = service()
        results = svc.run_due_tasks(iso(data["now"]) if data.get("now") else None)
        return jsonify(processed=results)

    # --------------------------------------------------------- 时点查询

    @api.get("/channels/status")
    def channel_status():
        license_no = request.args.get("license_no")
        region = request.args.get("region")
        at = request.args.get("at")
        if not license_no or not region or not at:
            raise DomainError(
                "missing_fields", "查询参数 license_no、region、at 均必填"
            )
        svc = service()
        return jsonify(svc.status_at(license_no=license_no, region=region, at=iso(at)))

    return api
