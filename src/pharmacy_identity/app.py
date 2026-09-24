"""Flask 应用入口与续办/渠道缓冲 HTTP 边界。

路由只做 JSON 解析与错误翻译，全部领域规则位于
:mod:`pharmacy_identity.service`。所有写操作都要求显式业务身份字段
（如 ``created_by`` / ``signer``），服务自身不持有会话身份。
"""

from flask import Flask, jsonify, request
from sqlalchemy import text

from .database import create_database_engine
from .service import DomainError, RenewalService


def create_app(engine=None) -> Flask:
    app = Flask(__name__)
    storage = engine or create_database_engine()
    service = RenewalService(storage)

    @app.errorhandler(DomainError)
    def handle_domain_error(error: DomainError):
        return jsonify(error="domain_violation", detail=str(error)), 422

    @app.errorhandler(KeyError)
    def handle_missing_field(error: KeyError):
        return jsonify(error="missing_field", detail=str(error.args[0])), 400

    @app.get("/health")
    def health():
        with storage.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(status="ok", storage="sqlite")

    # -------------------------------------------------------------- 基础数据

    @app.post("/admin/regions")
    def register_region():
        body = request.get_json(force=True)
        service.register_region(body["code"], body["name"])
        return jsonify(region_code=body["code"]), 201

    @app.post("/admin/rule-versions")
    def register_rule_version():
        body = request.get_json(force=True)
        rule_id = service.register_rule_version(
            body["region_code"], body["version"], body["effective_from"],
            body.get("rules"),
        )
        return jsonify(rule_version_id=rule_id), 201

    @app.post("/admin/operators")
    def register_operator():
        body = request.get_json(force=True)
        operator_id = service.register_operator(body["code"], body["name"])
        return jsonify(operator_id=operator_id), 201

    @app.post("/admin/stores")
    def register_store():
        body = request.get_json(force=True)
        store_id = service.register_store(
            body["code"], body["name"], body["region_code"], body["operator_code"],
        )
        return jsonify(store_id=store_id), 201

    @app.post("/admin/licenses")
    def register_license():
        body = request.get_json(force=True)
        license_id = service.register_license(
            body["number"], body["store_code"], body["operator_code"],
            body["issued_at"], body["expires_at"],
        )
        return jsonify(license_id=license_id), 201

    # ------------------------------------------------------------- 续办案卷

    @app.post("/renewals/batches")
    def submit_batch():
        body = request.get_json(force=True)
        result = service.submit_batch(
            docket_no=body["docket_no"],
            store_code=body["store_code"],
            license_number=body["license_number"],
            operator_code=body["operator_code"],
            created_by=body["created_by"],
            materials_doc=body["materials"],
            submitted_at=body.get("submitted_at"),
        )
        code = 200 if result.get("deduplicated") else 201
        return jsonify(result), code

    @app.post("/renewals/<docket_no>/materials")
    def add_materials(docket_no: str):
        body = request.get_json(force=True)
        return jsonify(service.add_materials(docket_no, body["materials"]))

    @app.post("/renewals/<docket_no>/materials/<doc_key>/review")
    def resolve_material_review(docket_no: str, doc_key: str):
        body = request.get_json(force=True)
        return jsonify(service.resolve_material_review(
            docket_no, doc_key, body["resolution"]))

    @app.post("/renewals/<docket_no>/events")
    def record_event(docket_no: str):
        body = request.get_json(force=True)
        result = service.record_event(
            docket_no=docket_no,
            kind=body["kind"],
            occurred_at=body["occurred_at"],
            payload=body.get("payload"),
            client_event_id=body.get("client_event_id"),
        )
        code = 200 if result.get("deduplicated") else 201
        return jsonify(result), code

    @app.post("/renewals/<docket_no>/signoffs")
    def sign_off(docket_no: str):
        body = request.get_json(force=True)
        return jsonify(service.sign_off(
            docket_no=docket_no, role=body["role"], signer=body["signer"])), 201

    # ------------------------------------------------------- 许可证监管动作

    @app.post("/licenses/<license_number>/actions")
    def record_license_action(license_number: str):
        body = request.get_json(force=True)
        result = service.record_license_action(
            license_number=license_number,
            kind=body["kind"],
            occurred_at=body["occurred_at"],
            detail=body.get("detail"),
        )
        return jsonify(result), 201

    # ------------------------------------------------------------- 渠道查询

    @app.get("/stores/<store_code>/channels")
    def channel_status(store_code: str):
        return jsonify(service.channel_status(store_code, request.args.get("as_of")))

    # ------------------------------------------------------------- 到期任务

    @app.post("/tasks/run-due")
    def run_due_tasks():
        body = request.get_json(silent=True) or {}
        return jsonify(service.run_due_tasks(body.get("as_of")))

    @app.get("/tasks/pending")
    def pending_tasks():
        return jsonify(tasks=service.pending_tasks())

    # ---------------------------------------------------------------- 订单

    @app.post("/orders")
    def complete_order():
        body = request.get_json(force=True)
        result = service.complete_order(
            order_no=body["order_no"],
            store_code=body["store_code"],
            channel=body["channel"],
            completed_at=body.get("completed_at"),
        )
        return jsonify(result), 201

    @app.get("/orders/<order_no>")
    def get_order(order_no: str):
        return jsonify(service.get_order(order_no))

    return app
