"""续办案卷与渠道缓冲领域服务。

所有写操作都在一个事务内完成，遵循：

* 事件/材料/动作只追加，现状行（案卷状态、渠道状态）的每次变化都留下历史；
* 裁定只依据 ``recorded_at <= 当前时刻`` 的事实，迟到回执进入日志但不倒改；
* 缓冲期限是绝对时间戳，落库为任务，停服恢复后由 :meth:`run_due_tasks` 续跑；
* 已成交订单冻结成交时状态，重评只能向其追加风险标记。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import and_, asc, desc, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from . import clock, rules
from .schema import (
    channel_states,
    channel_tasks,
    dossier_effectiveness,
    dossier_revisions,
    dossiers,
    events,
    license_actions,
    license_periods,
    licenses,
    materials,
    material_revisions,
    operators,
    order_risk_flags,
    orders,
    regions,
    rule_versions,
    signoffs,
    stores,
)

REGULATORY_EVENT_KINDS = {
    "accepted",
    "correction_request",
    "answered",
    "rejected",
    "approved",
    "license_issued",
    "withdrawn",
}
TERMINAL_EVENTS = {"rejected", "withdrawn", "license_issued"}
# 重评关闭/受限渠道时，对已成交订单追加的风险标记种类。
_STAGE_FLAG = {
    rules.SUSPENDED: "license_suspended",
    rules.TRANSFERRED: "license_transferred",
    rules.GRACE_EXPIRED: "grace_expired",
    rules.CORRECTION_OVERDUE: "correction_overdue",
    rules.REJECTED: "renewal_rejected",
    rules.WITHDRAWN: "renewal_withdrawn",
    rules.NO_RENEWAL: "license_expired_no_renewal",
}


class DomainError(Exception):
    """请求与领域不变量冲突（4xx）。"""


def _first_mapping(result):
    row = result.first()
    return row._mapping if row is not None else None


def _one_mapping(result):
    return result.one()._mapping


class RenewalService:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ------------------------------------------------------------------ 基础登记

    def register_region(self, code: str, name: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sqlite_insert(regions).values(code=code, name=name)
                .on_conflict_do_update(index_elements=[regions.c.code],
                                       set_={"name": name})
            )

    def register_rule_version(self, region_code: str, version: str,
                              effective_from: str, rules_doc: Mapping | None = None) -> int:
        """登记地区规则版本。同一 (地区, 版本) 重复登记即更新规则内容。"""
        doc = dict(rules.DEFAULT_RULES)
        if rules_doc:
            doc.update(rules_doc)
        with self.engine.begin() as conn:
            if conn.execute(select(regions.c.code).where(regions.c.code == region_code)).first() is None:
                raise DomainError(f"未知地区: {region_code}")
            conn.execute(
                sqlite_insert(rule_versions)
                .values(region_code=region_code, version=version,
                        effective_from=effective_from, rules=doc)
                .on_conflict_do_update(
                    index_elements=[rule_versions.c.region_code, rule_versions.c.version],
                    set_={"rules": doc, "effective_from": effective_from},
                )
            )
            row = conn.execute(
                select(rule_versions.c.id).where(
                    rule_versions.c.region_code == region_code,
                    rule_versions.c.version == version,
                )
            ).one()
            return row[0]

    def register_operator(self, code: str, name: str) -> int:
        with self.engine.begin() as conn:
            conn.execute(
                sqlite_insert(operators)
                .values(code=code, name=name, created_at=clock.now())
                .on_conflict_do_update(index_elements=[operators.c.code],
                                       set_={"name": name})
            )
            return conn.execute(select(operators.c.id).where(operators.c.code == code)).one()[0]

    def register_store(self, code: str, name: str, region_code: str, operator_code: str) -> int:
        with self.engine.begin() as conn:
            operator_id = self._operator_id(conn, operator_code)
            conn.execute(
                sqlite_insert(stores)
                .values(code=code, name=name, region_code=region_code,
                        operator_id=operator_id, created_at=clock.now())
                .on_conflict_do_update(index_elements=[stores.c.code],
                                       set_={"name": name, "region_code": region_code,
                                             "operator_id": operator_id})
            )
            return conn.execute(select(stores.c.id).where(stores.c.code == code)).one()[0]

    def register_license(self, number: str, store_code: str, operator_code: str,
                         issued_at: str, expires_at: str) -> int:
        with self.engine.begin() as conn:
            store = self._store(conn, store_code)
            operator_id = self._operator_id(conn, operator_code)
            if store["operator_id"] != operator_id:
                raise DomainError("许可证经营主体与门店登记主体不一致")
            conn.execute(
                sqlite_insert(licenses)
                .values(number=number, store_id=store["id"], operator_id=operator_id,
                        issued_at=issued_at, expires_at=expires_at, created_at=clock.now())
                .on_conflict_do_update(
                    index_elements=[licenses.c.number],
                    set_={"expires_at": expires_at, "operator_id": operator_id},
                )
            )
            license_id = conn.execute(
                select(licenses.c.id).where(licenses.c.number == number)
            ).one()[0]
            # 初始有效期仅登记一次；后续新证到达只追加 license_periods 行。
            existing_period = conn.execute(
                select(license_periods.c.id).where(
                    license_periods.c.license_id == license_id
                ).limit(1)
            ).first()
            if existing_period is None:
                conn.execute(
                    license_periods.insert().values(
                        license_id=license_id, valid_from=issued_at,
                        expires_at=expires_at, recorded_at=clock.now(),
                        source_event_id=None,
                    )
                )
            return license_id

    # ---------------------------------------------------------- 批次提交与材料

    def submit_batch(self, *, docket_no: str, store_code: str, license_number: str,
                     operator_code: str, created_by: str,
                     materials_doc: Sequence[Mapping],
                     submitted_at: str | None = None) -> dict:
        """门店按许可证与经营主体提交续办批次（幂等：同案卷号重发返回原案卷）。"""
        recorded = clock.now()
        happened = submitted_at or recorded
        with self.engine.begin() as conn:
            store = self._store(conn, store_code)
            license_row = self._license(conn, license_number)
            operator_id = self._operator_id(conn, operator_code)
            self._assert_batch_consistency(store, license_row, operator_id)

            existing = conn.execute(
                select(dossiers).where(dossiers.c.docket_no == docket_no)
            ).first()
            if existing is not None:
                return {"docket_no": docket_no, "id": existing[0], "deduplicated": True}

            rule_version_id = self._rule_version_at(conn, store["region_code"], happened)
            result = conn.execute(
                dossiers.insert().values(
                    docket_no=docket_no, license_id=license_row["id"],
                    store_id=store["id"], operator_id=operator_id,
                    region_code=store["region_code"], status="open", revision=1,
                    rule_version_id=rule_version_id, submitted_at=happened,
                    created_by=created_by,
                )
            )
            dossier_id = result.inserted_primary_key[0]
            self._append_event(conn, dossier_id, "submitted", happened, recorded,
                               {"docket_no": docket_no, "created_by": created_by},
                               client_event_id="submitted", bump_revision=False)
            for item in materials_doc:
                self._upsert_material(conn, dossier_id, item, recorded, happened)

            self._acquire_effectiveness(conn, license_row["id"], dossier_id, recorded)
            conn.execute(
                dossier_revisions.insert().values(
                    dossier_id=dossier_id, revision=1, reason="submitted",
                    changed_at=recorded,
                )
            )
            self._reevaluate(conn, store["id"], recorded)
            return {"docket_no": docket_no, "id": dossier_id, "deduplicated": False}

    def add_materials(self, docket_no: str, materials_doc: Sequence[Mapping]) -> dict:
        """补送材料：相同文件沿用原记录，同标识内容变化进入核查。"""
        recorded = clock.now()
        with self.engine.begin() as conn:
            dossier = self._dossier(conn, docket_no)
            changed = 0
            for item in materials_doc:
                changed += self._upsert_material(conn, dossier["id"], item, recorded, recorded)
            if changed:
                self._bump_revision(conn, dossier["id"], reason="material", at=recorded)
            self._reevaluate(conn, dossier["store_id"], recorded)
            return {"docket_no": docket_no, "changed": changed,
                    "resubmitted": len(materials_doc) - changed}

    def _upsert_material(self, conn: Connection, dossier_id: int, item: Mapping,
                         recorded: str, valid_default: str) -> int:
        """返回 1 表示新增/内容变化（影响案卷版本），0 表示相同文件重送。"""
        doc_key = item["doc_key"]
        content_hash = item["content_hash"]
        source_summary = item["source_summary"]
        valid_from = item.get("valid_from", valid_default)
        valid_until = item.get("valid_until")

        existing = conn.execute(
            select(materials).where(
                materials.c.dossier_id == dossier_id,
                materials.c.doc_key == doc_key,
            )
        ).first()
        existing = existing._mapping if existing is not None else None

        if existing is None:
            result = conn.execute(
                materials.insert().values(
                    dossier_id=dossier_id, doc_key=doc_key,
                    source_summary=source_summary, content_hash=content_hash,
                    valid_from=valid_from, valid_until=valid_until,
                    state="accepted", first_seen_at=recorded,
                    last_resubmitted_at=recorded, resubmit_count=1,
                )
            )
            conn.execute(
                material_revisions.insert().values(
                    material_id=result.inserted_primary_key[0],
                    content_hash=content_hash, source_summary=source_summary,
                    valid_from=valid_from, valid_until=valid_until,
                    changed_at=recorded,
                )
            )
            return 1

        if existing["content_hash"] == content_hash:
            # 相同文件重送：沿用原记录，仅留重送痕迹。
            conn.execute(
                materials.update()
                .where(materials.c.id == existing["id"])
                .values(last_resubmitted_at=recorded,
                        resubmit_count=materials.c.resubmit_count + 1)
            )
            return 0

        # 同一标识内容变化：留版本历史并进入核查。
        conn.execute(
            material_revisions.insert().values(
                material_id=existing["id"], content_hash=content_hash,
                source_summary=source_summary, valid_from=valid_from,
                valid_until=valid_until, changed_at=recorded,
            )
        )
        conn.execute(
            materials.update()
            .where(materials.c.id == existing["id"])
            .values(content_hash=content_hash, source_summary=source_summary,
                    valid_from=valid_from, valid_until=valid_until,
                    state="under_review", review_resolved_at=None,
                    last_resubmitted_at=recorded,
                    resubmit_count=materials.c.resubmit_count + 1)
        )
        return 1

    def resolve_material_review(self, docket_no: str, doc_key: str,
                                resolution: str) -> dict:
        """核查结论：通过则材料恢复 accepted（案卷进入新版本），驳回维持核查中。"""
        if resolution not in ("accepted", "rejected"):
            raise DomainError("核查结论必须是 accepted 或 rejected")
        recorded = clock.now()
        with self.engine.begin() as conn:
            dossier = self._dossier(conn, docket_no)
            material = _first_mapping(conn.execute(
                select(materials).where(
                    materials.c.dossier_id == dossier["id"],
                    materials.c.doc_key == doc_key,
                )
            ))
            if material is None:
                raise DomainError(f"材料不存在: {doc_key}")
            if resolution == "accepted" and material["state"] == "under_review":
                conn.execute(
                    materials.update().where(materials.c.id == material["id"])
                    .values(state="accepted", review_resolved_at=recorded)
                )
                self._bump_revision(conn, dossier["id"], reason="material", at=recorded)
            self._reevaluate(conn, dossier["store_id"], recorded)
            return {"docket_no": docket_no, "doc_key": doc_key,
                    "state": "accepted" if resolution == "accepted" else "under_review"}

    # -------------------------------------------------------------- 监管事件

    def record_event(self, *, docket_no: str, kind: str, occurred_at: str,
                     payload: Mapping | None = None,
                     client_event_id: str | None = None) -> dict:
        """追加监管受理/补正/驳回/批准/发证事件。事件只追加，不可修改。"""
        if kind not in REGULATORY_EVENT_KINDS:
            raise DomainError(f"不支持的监管事件类型: {kind}")
        recorded = clock.now()
        payload = dict(payload or {})
        with self.engine.begin() as conn:
            dossier = self._dossier(conn, docket_no)
            if dossier["status"] != "open":
                raise DomainError(f"案卷 {docket_no} 已终结({dossier['status']})，不能再追加事件")

            if client_event_id:
                dup = _first_mapping(conn.execute(
                    select(events).where(
                        events.c.dossier_id == dossier["id"],
                        events.c.client_event_id == client_event_id,
                    )
                ))
                if dup is not None:
                    return {"docket_no": docket_no, "event_id": dup["id"], "deduplicated": True}

            event_id = self._append_event(
                conn, dossier["id"], kind, occurred_at, recorded, payload,
                client_event_id=client_event_id, bump_revision=True,
            )

            if kind == "accepted":
                self._schedule_grace(conn, dossier, occurred_at, recorded)
            elif kind == "correction_request":
                self._schedule_task(conn, dossier, "correction_due", occurred_at,
                                    recorded, days_key="correction_days")
            elif kind == "answered":
                conn.execute(
                    channel_tasks.update()
                    .where(
                        channel_tasks.c.dossier_id == dossier["id"],
                        channel_tasks.c.kind == "correction_due",
                        channel_tasks.c.status == "pending",
                    )
                    .values(status="cancelled", processed_at=recorded,
                            result={"reason": "correction_answered"})
                )
            elif kind == "license_issued":
                conn.execute(
                    dossiers.update().where(dossiers.c.id == dossier["id"])
                    .values(status="granted", decided_at=recorded)
                )
                if payload.get("new_expires_at"):
                    # 新证到达：只追加有效期行，不倒改旧证截止日（as-of 可重放）。
                    conn.execute(
                        license_periods.insert().values(
                            license_id=dossier["license_id"],
                            valid_from=occurred_at,
                            expires_at=payload["new_expires_at"],
                            recorded_at=recorded, source_event_id=event_id,
                        )
                    )
                self._release_effectiveness(conn, dossier["license_id"], dossier["id"],
                                            recorded, "license_issued", promote=False)
                # 新证已到：同一许可证的其他争抢案卷失去意义，终结它们。
                conn.execute(
                    dossiers.update()
                    .where(
                        dossiers.c.license_id == dossier["license_id"],
                        dossiers.c.id != dossier["id"],
                        dossiers.c.status == "open",
                    )
                    .values(status="withdrawn", decided_at=recorded)
                )
                self._cancel_pending(conn, dossier["id"], recorded, "license_issued")
                self._cancel_pending_for_license(conn, dossier["license_id"],
                                                 recorded, "license_issued_other_dossier")
            elif kind in ("rejected", "withdrawn"):
                conn.execute(
                    dossiers.update().where(dossiers.c.id == dossier["id"])
                    .values(status=kind, decided_at=recorded)
                )
                self._release_effectiveness(conn, dossier["license_id"], dossier["id"],
                                            recorded, kind, promote=True)
                self._cancel_pending(conn, dossier["id"], recorded, kind)

            self._reevaluate(conn, dossier["store_id"], recorded)
            return {"docket_no": docket_no, "event_id": event_id, "deduplicated": False}

    def _append_event(self, conn: Connection, dossier_id: int, kind: str,
                      occurred_at: str, recorded_at: str, payload: Mapping,
                      *, client_event_id: str | None, bump_revision: bool) -> int:
        next_seq = conn.execute(
            select(func.coalesce(func.max(events.c.seq), 0) + 1)
            .where(events.c.dossier_id == dossier_id)
        ).scalar_one()
        result = conn.execute(
            events.insert().values(
                dossier_id=dossier_id, seq=next_seq, kind=kind,
                occurred_at=occurred_at, recorded_at=recorded_at,
                payload=dict(payload), client_event_id=client_event_id,
            )
        )
        if bump_revision:
            self._bump_revision(conn, dossier_id, at=recorded_at)
        return result.inserted_primary_key[0]

    def _bump_revision(self, conn: Connection, dossier_id: int,
                       reason: str = "event", at: str | None = None) -> None:
        at = at or clock.now()
        new_value = conn.execute(
            select(func.coalesce(func.max(dossiers.c.revision), 0))
            .where(dossiers.c.id == dossier_id)
        ).scalar_one() + 1
        conn.execute(
            dossiers.update().where(dossiers.c.id == dossier_id)
            .values(revision=new_value)
        )
        conn.execute(
            dossier_revisions.insert().values(
                dossier_id=dossier_id, revision=new_value,
                reason=reason, changed_at=at,
            )
        )

    # ------------------------------------------------------ 许可证暂停与转让

    def record_license_action(self, *, license_number: str, kind: str,
                              occurred_at: str, detail: Mapping | None = None) -> dict:
        """暂停/恢复/转让只追加；暂停或转让立即重评该许可证所有未完成渠道。"""
        if kind not in ("suspended", "reinstated", "transferred"):
            raise DomainError(f"不支持的许可证动作: {kind}")
        recorded = clock.now()
        with self.engine.begin() as conn:
            license_row = self._license(conn, license_number)
            result = conn.execute(
                license_actions.insert().values(
                    license_id=license_row["id"], kind=kind,
                    occurred_at=occurred_at, recorded_at=recorded,
                    detail=dict(detail or {}),
                )
            )
            if kind == "transferred" and detail and detail.get("new_operator_code"):
                new_operator_id = self._operator_id(conn, detail["new_operator_code"])
                conn.execute(
                    licenses.update().where(licenses.c.id == license_row["id"])
                    .values(operator_id=new_operator_id)
                )
            self._reevaluate(conn, license_row["store_id"], recorded)
            return {"license_number": license_number, "action_id": result.inserted_primary_key[0]}

    # ---------------------------------------------------------------- 双签

    def sign_off(self, *, docket_no: str, role: str, signer: str) -> dict:
        """合规人员或业务负责人对当前案卷版本签署。发起人不能自批。"""
        if role not in ("compliance", "business"):
            raise DomainError("签署角色必须是 compliance 或 business")
        recorded = clock.now()
        with self.engine.begin() as conn:
            dossier = self._dossier(conn, docket_no)
            if signer == dossier["created_by"]:
                raise DomainError("发起人不能审批自己发起的续办案卷")
            if not self._is_effective(conn, dossier["license_id"], dossier["id"], recorded):
                raise DomainError("该案卷不是许可证当前唯一生效案卷，不能签署放行")

            other = "business" if role == "compliance" else "compliance"
            existing_other = _first_mapping(conn.execute(
                select(signoffs).where(
                    signoffs.c.dossier_id == dossier["id"],
                    signoffs.c.revision == dossier["revision"],
                    signoffs.c.role == other,
                )
            ))
            if existing_other is not None and existing_other["signer"] == signer:
                raise DomainError("合规人员与业务负责人必须是两个不同的人")

            result = conn.execute(
                sqlite_insert(signoffs)
                .values(dossier_id=dossier["id"], revision=dossier["revision"],
                        role=role, signer=signer, signed_at=recorded,
                        recorded_at=recorded)
                .on_conflict_do_nothing(
                    index_elements=[signoffs.c.dossier_id, signoffs.c.revision,
                                    signoffs.c.role])
            )
            self._reevaluate(conn, dossier["store_id"], recorded)
            return {"docket_no": docket_no, "role": role, "signer": signer,
                    "revision": dossier["revision"],
                    "inserted": result.rowcount != 0}

    # ------------------------------------------------------------ 渠道重评

    def _reevaluate(self, conn: Connection, store_id: int, as_of: str) -> None:
        """按 as_of 重评三渠道，状态/理由变化即追加裁定行，并给历史订单加风险标记。"""
        snapshot = self._build_live_snapshot(conn, store_id, as_of)
        evaluation = rules.evaluate(snapshot, as_of)
        material_snapshot = [
            {
                "id": m["id"], "doc_key": m["doc_key"],
                "source_summary": m["source_summary"],
                "content_hash": m["content_hash"], "state": m["state"],
                "valid_from": m["valid_from"], "valid_until": m.get("valid_until"),
            }
            for m in snapshot["materials"]
        ]

        for channel, verdict in evaluation.items():
            current = _first_mapping(conn.execute(
                select(channel_states)
                .where(
                    channel_states.c.store_id == store_id,
                    channel_states.c.channel == channel,
                    channel_states.c.superseded_at.is_(None),
                )
                .order_by(desc(channel_states.c.effective_at))
            ))

            if current is not None and current["state"] == verdict["state"] \
                    and current["reason"] == verdict["reason"]:
                continue

            if current is not None:
                conn.execute(
                    channel_states.update()
                    .where(channel_states.c.id == current["id"])
                    .values(superseded_at=as_of)
                )
            conn.execute(
                channel_states.insert().values(
                    store_id=store_id, channel=channel,
                    state=verdict["state"], reason=verdict["reason"],
                    rule_version_id=snapshot["rules"]["id"],
                    dossier_id=snapshot["dossier"]["id"],
                    based_on_revision=snapshot["dossier"]["revision"],
                    material_ids=verdict["material_ids"],
                    material_snapshot=material_snapshot,
                    event_ids=verdict["event_ids"],
                    effective_at=as_of,
                )
            )
            self._flag_completed_orders(conn, store_id, channel, verdict, as_of)

    def _flag_completed_orders(self, conn: Connection, store_id: int,
                               channel: str, verdict: Mapping, as_of: str) -> None:
        flag_kind = _STAGE_FLAG.get(verdict["stage"])
        if flag_kind is None or verdict["state"] == rules.OPEN:
            return
        rows = conn.execute(
            select(orders.c.id).where(
                orders.c.store_id == store_id,
                orders.c.channel == channel,
                orders.c.completed_at <= as_of,
            )
        ).all()
        for row in rows:
            conn.execute(
                sqlite_insert(order_risk_flags)
                .values(order_id=row[0], kind=flag_kind,
                        reason=f"渠道于 {as_of} 重评为 {verdict['state']}（{verdict['reason']}）",
                        flagged_at=as_of)
                .on_conflict_do_nothing(
                    index_elements=[order_risk_flags.c.order_id, order_risk_flags.c.kind])
            )

    # ------------------------------------------------------- 停服恢复/到期任务

    def run_due_tasks(self, as_of: str | None = None) -> dict:
        """执行所有到期任务。宕机期间错过的任务会在恢复后一次性补齐。"""
        as_of = as_of or clock.now()
        processed = []
        with self.engine.begin() as conn:
            pending = conn.execute(
                select(channel_tasks)
                .where(
                    channel_tasks.c.status == "pending",
                    channel_tasks.c.due_at <= as_of,
                )
                .order_by(asc(channel_tasks.c.due_at))
            ).all()
            for task in pending:
                task = task._mapping
                result = {"ran_at": as_of, "scheduled_for": task["due_at"]}
                if task["kind"] == "notify":
                    result["notification"] = (
                        f"{task['channel']} 渠道 {task['kind']} 到期提醒已于 {as_of} 产生"
                    )
                # 所有到期任务本质都是“唤醒一次重评”；裁定由当前事实与时钟决定。
                self._reevaluate(conn, task["store_id"], as_of)
                conn.execute(
                    channel_tasks.update()
                    .where(channel_tasks.c.id == task["id"])
                    .values(status="done", processed_at=as_of, result=result)
                )
                processed.append({"task_id": task["id"], "kind": task["kind"],
                                  "channel": task["channel"], "due_at": task["due_at"]})
        return {"ran_at": as_of, "processed": processed, "count": len(processed)}

    def _schedule_grace(self, conn: Connection, dossier, occurred_at: str,
                        recorded: str) -> None:
        rules_doc = self._rules_by_id(conn, dossier["rule_version_id"])
        grace_end = clock.add_days(occurred_at, rules_doc["grace_days"])
        for channel in rules.CHANNELS:
            self._insert_task(conn, dossier, channel, "grace_expiry", grace_end, recorded)
            notify_at = clock.add_days(occurred_at, max(rules_doc["grace_days"] - 2, 0))
            self._insert_task(conn, dossier, channel, "notify", notify_at, recorded)

    def _schedule_task(self, conn: Connection, dossier, kind: str, occurred_at: str,
                       recorded: str, *, days_key: str) -> None:
        rules_doc = self._rules_by_id(conn, dossier["rule_version_id"])
        due = clock.add_days(occurred_at, rules_doc[days_key])
        for channel in rules.CHANNELS:
            self._insert_task(conn, dossier, channel, kind, due, recorded)

    def _insert_task(self, conn, dossier, channel: str, kind: str,
                     due_at: str, recorded: str) -> None:
        conn.execute(
            sqlite_insert(channel_tasks)
            .values(store_id=dossier["store_id"], channel=channel, kind=kind,
                    due_at=due_at, dossier_id=dossier["id"], created_at=recorded)
            .on_conflict_do_nothing(
                index_elements=[channel_tasks.c.store_id, channel_tasks.c.channel,
                                channel_tasks.c.kind, channel_tasks.c.dossier_id])
        )

    def _cancel_pending(self, conn, dossier_id: int, at: str, reason: str) -> None:
        conn.execute(
            channel_tasks.update()
            .where(
                channel_tasks.c.dossier_id == dossier_id,
                channel_tasks.c.status == "pending",
            )
            .values(status="cancelled", processed_at=at, result={"reason": reason})
        )

    def _cancel_pending_for_license(self, conn, license_id: int,
                                    at: str, reason: str) -> None:
        conn.execute(
            channel_tasks.update()
            .where(
                channel_tasks.c.status == "pending",
                channel_tasks.c.dossier_id.in_(
                    select(dossiers.c.id).where(dossiers.c.license_id == license_id)
                ),
            )
            .values(status="cancelled", processed_at=at, result={"reason": reason})
        )

    # ------------------------------------------------------------- 竞争裁决

    def _acquire_effectiveness(self, conn: Connection, license_id: int,
                               dossier_id: int, recorded: str) -> None:
        """竞争唯一生效位：先到先得。

        以系统记录时点为准，最先提交的案卷取得唯一生效位；后来的争抢案卷
        照常记录其事件与材料，但不能挤掉在位者，因此迟到回执不会倒改已经
        基于在位案卷作出的决定。在位案卷终结后再按提交顺序晋升下一个。
        """
        if self._open_effectiveness(conn, license_id) is not None:
            return
        # 并发提交下由部分唯一索引兜底：DO NOTHING 保证后来者抢不到唯一生效位，
        # 也不会让唯一约束异常中止当前事务。
        conn.execute(
            sqlite_insert(dossier_effectiveness)
            .values(license_id=license_id, dossier_id=dossier_id, acquired_at=recorded)
            .on_conflict_do_nothing(
                index_elements=[dossier_effectiveness.c.license_id],
                index_where=dossier_effectiveness.c.released_at.is_(None),
            )
        )

    def _release_effectiveness(self, conn: Connection, license_id: int,
                               dossier_id: int, at: str, reason: str,
                               *, promote: bool) -> None:
        conn.execute(
            dossier_effectiveness.update()
            .where(
                dossier_effectiveness.c.license_id == license_id,
                dossier_effectiveness.c.dossier_id == dossier_id,
                dossier_effectiveness.c.released_at.is_(None),
            )
            .values(released_at=at, release_reason=reason)
        )
        if promote:
            self._promote_effectiveness(conn, license_id, at)

    def _promote_effectiveness(self, conn: Connection, license_id: int, at: str) -> None:
        if self._open_effectiveness(conn, license_id) is not None:
            return
        candidate = _first_mapping(conn.execute(
            select(dossiers)
            .where(
                dossiers.c.license_id == license_id,
                dossiers.c.status == "open",
            )
            .order_by(asc(dossiers.c.submitted_at), asc(dossiers.c.id))
            .limit(1)
        ))
        if candidate is not None:
            conn.execute(
                dossier_effectiveness.insert().values(
                    license_id=license_id, dossier_id=candidate["id"], acquired_at=at
                )
            )

    def _open_effectiveness(self, conn: Connection, license_id: int):
        return _first_mapping(conn.execute(
            select(dossier_effectiveness)
            .where(
                dossier_effectiveness.c.license_id == license_id,
                dossier_effectiveness.c.released_at.is_(None),
            )
        ))

    def _is_effective(self, conn: Connection, license_id: int,
                      dossier_id: int, at: str) -> bool:
        holder = self._open_effectiveness(conn, license_id)
        return holder is not None and holder["dossier_id"] == dossier_id

    # ------------------------------------------------------------- 订单

    def complete_order(self, *, order_no: str, store_code: str, channel: str,
                       completed_at: str | None = None) -> dict:
        """成交订单：冻结成交时点渠道状态作为下单依据。"""
        recorded = clock.now()
        happened = completed_at or recorded
        with self.engine.begin() as conn:
            store = self._store(conn, store_code)
            snapshot = self._build_snapshot_as_of(conn, store["id"], happened)
            verdict = rules.evaluate(snapshot, happened)[channel]
            conn.execute(
                sqlite_insert(orders)
                .values(order_no=order_no, store_id=store["id"], channel=channel,
                        completed_at=happened, snapshot_state=verdict["state"],
                        snapshot_reason=verdict["reason"], created_at=recorded)
                .on_conflict_do_nothing(index_elements=[orders.c.order_no])
            )
            return {"order_no": order_no, "channel": channel,
                    "completed_at": happened, "state": verdict["state"],
                    "reason": verdict["reason"]}

    def get_order(self, order_no: str) -> dict:
        with self.engine.begin() as conn:
            order = _first_mapping(
                conn.execute(select(orders).where(orders.c.order_no == order_no)))
            if order is None:
                raise DomainError(f"订单不存在: {order_no}")
            flags = conn.execute(
                select(order_risk_flags).where(order_risk_flags.c.order_id == order["id"])
                .order_by(asc(order_risk_flags.c.flagged_at))
            ).all()
            return {
                "order_no": order["order_no"], "channel": order["channel"],
                "completed_at": order["completed_at"],
                "snapshot_state": order["snapshot_state"],
                "snapshot_reason": order["snapshot_reason"],
                "risk_flags": [{"kind": f._mapping["kind"], "reason": f._mapping["reason"],
                                "flagged_at": f._mapping["flagged_at"]} for f in flags],
            }

    # ------------------------------------------------------------- 查询解释

    def channel_status(self, store_code: str, as_of: str | None = None) -> dict:
        """解释任一日期渠道为何开放/受限/关闭，以及采用了哪些材料与规则。"""
        as_of = as_of or clock.now()
        with self.engine.begin() as conn:
            store = self._store(conn, store_code)
            snapshot = self._build_snapshot_as_of(conn, store["id"], as_of)
            evaluation = rules.evaluate(snapshot, as_of)

            rulings = {}
            for channel in rules.CHANNELS:
                row = _first_mapping(conn.execute(
                    select(channel_states)
                    .where(
                        channel_states.c.store_id == store["id"],
                        channel_states.c.channel == channel,
                        channel_states.c.effective_at <= as_of,
                    )
                    .order_by(desc(channel_states.c.effective_at))
                    .limit(1)
                ))
                rulings[channel] = self._ruling_basis(conn, row) if row else None

            return {
                "store_code": store_code,
                "as_of": as_of,
                "docket_no": snapshot["dossier"].get("docket_no"),
                "dossier_revision": snapshot["dossier"]["revision"],
                "rule_version": snapshot["rules"]["version"],
                "rule_region": snapshot["rules"]["region_code"],
                "channels": evaluation,
                "persisted_rulings": rulings,
                "known_events": self._event_ledger(conn, snapshot["dossier"]["id"], as_of),
            }

    def _ruling_basis(self, conn: Connection, row) -> dict:
        # 材料依据优先使用裁定时刻冻结的快照，材料日后变化不倒改历史裁定。
        frozen = row["material_snapshot"]
        material_rows = list(frozen) if frozen else []
        if not frozen and row["material_ids"]:
            material_rows = [
                dict(r._mapping)
                for r in conn.execute(
                    select(materials.c.doc_key, materials.c.source_summary,
                           materials.c.content_hash, materials.c.state)
                    .where(materials.c.id.in_(row["material_ids"]))
                ).all()
            ]
        event_rows = conn.execute(
            select(events.c.seq, events.c.kind, events.c.occurred_at,
                   events.c.recorded_at)
            .where(events.c.id.in_(row["event_ids"] or []))
            .order_by(asc(events.c.seq))
        ).all() if row["event_ids"] else []
        rv = _one_mapping(conn.execute(
            select(rule_versions.c.version, rule_versions.c.region_code)
            .where(rule_versions.c.id == row["rule_version_id"])
        ))
        return {
            "state": row["state"], "reason": row["reason"],
            "effective_at": row["effective_at"],
            "superseded_at": row["superseded_at"],
            "based_on_revision": row["based_on_revision"],
            "rule_version": rv["version"], "region": rv["region_code"],
            "materials": material_rows,
            "events": [dict(r._mapping) for r in event_rows],
        }

    def _event_ledger(self, conn: Connection, dossier_id: int | None, as_of: str) -> list:
        if dossier_id is None:
            return []
        rows = conn.execute(
            select(events.c.seq, events.c.kind, events.c.occurred_at,
                   events.c.recorded_at, events.c.payload)
            .where(
                events.c.dossier_id == dossier_id,
                events.c.recorded_at <= as_of,
            )
            .order_by(asc(events.c.seq))
        ).all()
        return [dict(r._mapping) for r in rows]

    def pending_tasks(self) -> list:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(channel_tasks).where(channel_tasks.c.status == "pending")
                .order_by(asc(channel_tasks.c.due_at))
            ).all()
            return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------- 快照组装

    def _build_live_snapshot(self, conn: Connection, store_id: int, as_of: str) -> dict:
        """重评用：当前生效案卷 + 截至 as_of 已记录事实。"""
        license_row = _one_mapping(conn.execute(
            select(licenses).where(licenses.c.store_id == store_id)
        ))
        holder = self._open_effectiveness(conn, license_row["id"])
        dossier = None
        if holder is not None:
            dossier = _one_mapping(conn.execute(
                select(dossiers).where(dossiers.c.id == holder["dossier_id"])
            ))
        return self._assemble(conn, license_row, dossier, as_of)

    def _build_snapshot_as_of(self, conn: Connection, store_id: int, as_of: str) -> dict:
        """as-of 重放：依据生效历史定位当时唯一生效案卷，只取当时已记录的事实。"""
        license_row = _one_mapping(conn.execute(
            select(licenses).where(licenses.c.store_id == store_id)
        ))
        entry = _first_mapping(conn.execute(
            select(dossier_effectiveness)
            .where(
                dossier_effectiveness.c.license_id == license_row["id"],
                dossier_effectiveness.c.acquired_at <= as_of,
                and_(
                    (dossier_effectiveness.c.released_at.is_(None))
                    | (dossier_effectiveness.c.released_at > as_of)
                ),
            )
            .order_by(desc(dossier_effectiveness.c.acquired_at))
            .limit(1)
        ))
        dossier = None
        if entry is not None:
            dossier = _one_mapping(conn.execute(
                select(dossiers).where(dossiers.c.id == entry["dossier_id"])
            ))
        return self._assemble(conn, license_row, dossier, as_of)

    def _assemble(self, conn: Connection, license_row, dossier, as_of: str) -> dict:
        if dossier is None:
            region_code = self._region_of(conn, license_row)
            rule_version = _first_mapping(conn.execute(
                select(rule_versions)
                .where(
                    rule_versions.c.region_code == region_code,
                    rule_versions.c.effective_from <= as_of,
                )
                .order_by(desc(rule_versions.c.effective_from))
                .limit(1)
            ))
            if rule_version is None:
                raise DomainError(f"地区在 {as_of} 没有可用规则版本")
            dossier_map = None
            event_rows, material_rows, signoff_rows = [], [], []
        else:
            rule_version = _one_mapping(conn.execute(
                select(rule_versions).where(
                    rule_versions.c.id == dossier["rule_version_id"])
            ))
            # 还原 as_of 当时的案卷版本（最新一个 changed_at <= as_of 的版本）。
            revision_then = conn.execute(
                select(dossier_revisions.c.revision)
                .where(
                    dossier_revisions.c.dossier_id == dossier["id"],
                    dossier_revisions.c.changed_at <= as_of,
                )
                .order_by(desc(dossier_revisions.c.revision))
                .limit(1)
            ).scalar()
            dossier_map = dict(dossier)
            if revision_then is not None:
                dossier_map["revision"] = revision_then
            event_rows = conn.execute(
                select(events).where(
                    events.c.dossier_id == dossier["id"],
                    events.c.recorded_at <= as_of,
                )
            ).all()
            material_rows = self._materials_as_of(conn, dossier["id"], as_of)
            # 签署只按当时已落库、且绑定当时版本的签署计入。
            signoff_rows = conn.execute(
                select(signoffs).where(
                    signoffs.c.dossier_id == dossier["id"],
                    signoffs.c.recorded_at <= as_of,
                    signoffs.c.revision == (revision_then or dossier["revision"]),
                )
            ).all() if revision_then is not None else []

        action_rows = conn.execute(
            select(license_actions).where(
                license_actions.c.license_id == license_row["id"],
                license_actions.c.recorded_at <= as_of,
            )
        ).all()

        # 按 as_of 还原当时许可证截止日（新证到达只追加有效期行）。
        license_map = dict(license_row)
        period = _first_mapping(conn.execute(
            select(license_periods)
            .where(
                license_periods.c.license_id == license_row["id"],
                license_periods.c.recorded_at <= as_of,
                license_periods.c.valid_from <= as_of,
            )
            .order_by(desc(license_periods.c.recorded_at))
            .limit(1)
        ))
        if period is not None:
            license_map["expires_at"] = period["expires_at"]
            license_map["issued_at"] = period["valid_from"]

        rules_doc = dict(rule_version["rules"])
        rules_doc["id"] = rule_version["id"]
        rules_doc["version"] = rule_version["version"]
        rules_doc["region_code"] = rule_version["region_code"]

        return rules.assemble_snapshot(
            license_row=license_map,
            dossier_row=dossier_map,
            rules=rules_doc,
            events=[dict(r._mapping) for r in event_rows],
            materials=material_rows,
            signoffs=[dict(r._mapping) for r in signoff_rows],
            actions=[dict(r._mapping) for r in action_rows],
        )

    def _region_of(self, conn: Connection, license_row) -> str:
        return conn.execute(
            select(stores.c.region_code).where(stores.c.id == license_row["store_id"])
        ).scalar_one()

    def _materials_as_of(self, conn: Connection, dossier_id: int, as_of: str) -> list[dict]:
        """材料的 as-of 状态：内容变化即核查中，核查通过后恢复 accepted。"""
        rows = conn.execute(
            select(materials).where(
                materials.c.dossier_id == dossier_id,
                materials.c.first_seen_at <= as_of,
            )
        ).all()
        result = []
        for row in rows:
            row = row._mapping
            data = dict(row)
            revision_count = conn.execute(
                select(func.count())
                .select_from(material_revisions)
                .where(
                    material_revisions.c.material_id == row["id"],
                    material_revisions.c.changed_at <= as_of,
                )
            ).scalar_one()
            if revision_count >= 2:
                resolved = row["review_resolved_at"]
                data["state"] = "accepted" if resolved and resolved <= as_of else "under_review"
            else:
                data["state"] = "accepted"
            result.append(data)
        return result

    def _rules_by_id(self, conn: Connection, rule_version_id: int) -> dict:
        return _one_mapping(conn.execute(
            select(rule_versions).where(rule_versions.c.id == rule_version_id)
        ))["rules"]

    # ------------------------------------------------------------- 查找辅助

    def _store(self, conn: Connection, code: str):
        row = conn.execute(select(stores).where(stores.c.code == code)).first()
        if row is None:
            raise DomainError(f"门店不存在: {code}")
        return row._mapping

    def _license(self, conn: Connection, number: str):
        row = conn.execute(select(licenses).where(licenses.c.number == number)).first()
        if row is None:
            raise DomainError(f"许可证不存在: {number}")
        return row._mapping

    def _operator_id(self, conn: Connection, code: str) -> int:
        row = conn.execute(select(operators.c.id).where(operators.c.code == code)).first()
        if row is None:
            raise DomainError(f"经营主体不存在: {code}")
        return row[0]

    def _dossier(self, conn: Connection, docket_no: str):
        row = conn.execute(
            select(dossiers).where(dossiers.c.docket_no == docket_no)
        ).first()
        if row is None:
            raise DomainError(f"案卷不存在: {docket_no}")
        return row._mapping

    def _rule_version_at(self, conn: Connection, region_code: str, at: str) -> int:
        row = conn.execute(
            select(rule_versions.c.id)
            .where(
                rule_versions.c.region_code == region_code,
                rule_versions.c.effective_from <= at,
            )
            .order_by(desc(rule_versions.c.effective_from))
            .limit(1)
        ).first()
        if row is None:
            raise DomainError(f"地区 {region_code} 在 {at} 没有可用规则版本")
        return row[0]

    def _assert_batch_consistency(self, store, license_row, operator_id: int) -> None:
        if license_row["store_id"] != store["id"]:
            raise DomainError("许可证与门店不匹配")
        if license_row["operator_id"] != operator_id or store["operator_id"] != operator_id:
            raise DomainError("续办批次的经营主体必须与许可证、门店一致")
